"""End-to-end fixture for the T035 real-mainnet ingestion path.

This test exercises the full chain:

    RpcEndpointClient.call_get_logs
        -> RpcBlockHeaderSource.get_headers_batch
            -> RawPartitionWriter.append_partition
                -> ManifestStore.upsert_block_header

The fixture stands up:

- a fake :class:`RpcAdapter` whose transport returns a single
  ``Initialize``-shaped ``eth_getLogs`` row spanning one block and
  one ``eth_getBlockByNumber(hex(n), false)`` header per distinct
  event block;
- the production :class:`RpcEndpointClient` driving
  ``call_get_logs``;
- the production :class:`RpcBlockHeaderSource` driving
  ``get_headers_batch``;
- a real :class:`ManifestStore` + :class:`RawPartitionWriter`;
- the production :class:`IngestionRunner` orchestrating the run.

The acceptance assertions (per the FAIL review):

- events persisted with non-zero ``block_timestamp`` and non-zero
  ``parent_hash``;
- the ``(block_number, block_hash, parent_hash, timestamp)`` tuple
  for each event block equals the values the source returns;
- the ``block_headers`` manifest table has exactly one row per
  distinct event block (dedup invariant);
- no ``eth_call`` is issued during acquisition.

The test is intentionally narrow: one bounded range, one event row,
one block. The goal is to prove the wiring rather than re-exercise
the routing fixtures from T032.
"""

from __future__ import annotations

from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from robinhood_lp.ingestion import (
    ALIAS_ROBINHOOD_PUBLIC,
    IngestionRunner,
    RangePlanner,
)
from robinhood_lp.ingestion.block_header_source import (
    BlockHeaderSink,
    RpcBlockHeaderSource,
)
from robinhood_lp.ingestion.endpoint_client import RpcEndpointClient
from robinhood_lp.ingestion.router import RouterConfig
from robinhood_lp.protocol import Address, ChainId, PoolId
from robinhood_lp.protocol.abi_artifacts import EVENT_TOPICS
from robinhood_lp.rpc.adapter import (
    RpcAdapter,
    RpcConfig,
    RpcEndpoint,
)
from robinhood_lp.storage import ManifestStore, RawPartitionWriter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAIN_ID = 4663
POOL_ID_INT = 0xABCDEF12
POOL_ID_HEX = "0x" + format(POOL_ID_INT, "064x")
POOL_INIT_BLOCK = 1_000_000
POOL_MANAGER = Address.from_hex("0x" + "55" * 20)


def _make_initialize_row(
    *,
    block_number: int,
    block_hash: int,
    transaction_hash: int,
    transaction_index: int = 0,
    log_index: int = 0,
) -> dict[str, Any]:
    """Build a valid V4 ``Initialize`` ``eth_getLogs`` row.

    ``Initialize`` has topic0 + three indexed topics + five data
    fields per the pinned v4-core ABI. We populate every field with
    deterministic non-zero values so the decoder succeeds and the
    typed record carries non-zero typed fields.
    """
    topic0 = EVENT_TOPICS["Initialize"]
    topics = [
        "0x" + topic0.hex(),
        POOL_ID_HEX,  # pool_id (indexed, 32 bytes)
        "0x" + "00" * 12 + "11" * 20,  # currency0 (indexed, address left-padded to 32 bytes)
        "0x" + "00" * 12 + "22" * 20,  # currency1 (indexed, address left-padded to 32 bytes)
    ]
    # data: fee (uint24), tick_spacing (int24), hooks (address),
    # sqrt_price_x96 (uint160), tick (int24) -- each 32 bytes
    fee = 3000
    tick_spacing = 60
    hooks_int = 0
    sqrt_price_x96 = 79228162514264337593543950336  # 2**96
    tick = 0
    data_bytes = (
        fee.to_bytes(32, "big")
        + (tick_spacing if tick_spacing >= 0 else (1 << 24) + tick_spacing).to_bytes(32, "big")
        + hooks_int.to_bytes(32, "big")
        + sqrt_price_x96.to_bytes(32, "big")
        + (tick if tick >= 0 else (1 << 24) + tick).to_bytes(32, "big")
    )
    return {
        "address": POOL_MANAGER.to_hex(),
        "topics": topics,
        "data": "0x" + data_bytes.hex(),
        "blockNumber": "0x" + format(block_number, "x"),
        "transactionHash": "0x" + format(transaction_hash, "064x"),
        "transactionIndex": "0x" + format(transaction_index, "x"),
        "logIndex": "0x" + format(log_index, "x"),
        "blockHash": "0x" + format(block_hash, "064x"),
        "removed": False,
    }


def _make_header_payload(
    *,
    block_number: int,
    block_hash: int,
    parent_hash: int,
    timestamp: int,
) -> dict[str, Any]:
    """Build a minimal non-hydrated ``eth_getBlockByNumber`` payload."""
    return {
        "hash": "0x" + format(block_hash, "064x"),
        "parentHash": "0x" + format(parent_hash, "064x"),
        "number": "0x" + format(block_number, "x"),
        "timestamp": "0x" + format(timestamp, "x"),
        "nonce": "0x0000000000000000",
        "sha3Uncles": "0x" + "00" * 32,
        "logsBloom": "0x" + "00" * 256,
        "transactionsRoot": "0x" + "00" * 32,
        "stateRoot": "0x" + "00" * 32,
        "receiptsRoot": "0x" + "00" * 32,
        "miner": "0x" + "00" * 20,
        "difficulty": "0x0",
        "totalDifficulty": "0x0",
        "extraData": "0x",
        "size": "0x0",
        "gasLimit": "0x0",
        "gasUsed": "0x0",
        "transactions": [],
    }


# ---------------------------------------------------------------------------
# Scripted transport
# ---------------------------------------------------------------------------


class _ScriptedTransport:
    """A scripted HTTP transport that handles every method the runner uses.

    The transport separates single-request payloads (eth_getLogs via
    the adapter's normal call path) from batch payloads
    (eth_getBlockByNumber via the source's batch path). For the
    batch path the source expects a payload of the form
    ``{"jsonrpc": "2.0", "batch": [...]}`` -- we detect that shape
    and reply with the canonical JSON-RPC batch response (a list).
    """

    def __init__(self) -> None:
        self.eth_get_logs_responses: list[list[dict[str, Any]]] = []
        self.eth_call_invocations: int = 0
        self.used_methods: list[str] = []

    def __call__(self, url: str, payload: dict[str, Any]) -> Awaitable[Any]:
        async def _run() -> Any:
            # Batch payload from RpcBlockHeaderSource._fetch_batched.
            if "batch" in payload:
                entries = payload["batch"]
                response_entries: list[dict[str, Any]] = []
                for entry in entries:
                    block_number = int(entry["params"][0], 16)
                    response_entries.append(_block_response_for(entry, block_number))
                self.used_methods.append("eth_getBlockByNumber")
                # The source's ``_parse_json_rpc_batch_response``
                # accepts either a list (JSON-RPC batch standard)
                # or a single dict (one-entry collapsed). We return
                # the canonical list shape.
                return response_entries
            method = payload.get("method")
            if method == "eth_getLogs":
                self.used_methods.append("eth_getLogs")
                rows = self.eth_get_logs_responses.pop(0)
                return {"jsonrpc": "2.0", "id": payload["id"], "result": rows}
            if method == "eth_call":
                self.eth_call_invocations += 1
                raise AssertionError("ingest must not issue eth_call during acquisition")
            raise AssertionError(f"unexpected RPC method: {method!r}")

        return _run()


# Map of pre-scripted block headers by block_number; populated by tests.
_HEADER_TABLE: dict[int, dict[str, Any]] = {}


def _block_response_for(entry: dict[str, Any], block_number: int) -> dict[str, Any]:
    payload = _HEADER_TABLE.get(block_number)
    if payload is None:
        raise AssertionError(f"unexpected eth_getBlockByNumber call for block {block_number}")
    return {
        "jsonrpc": "2.0",
        "id": entry["id"],
        "result": payload,
    }


# ---------------------------------------------------------------------------
# Runner fixture
# ---------------------------------------------------------------------------


def _build_runner(
    *,
    tmp_path: Path,
    transport: _ScriptedTransport,
    planner_max_blocks: int = 100,
) -> tuple[
    IngestionRunner,
    ManifestStore,
    RawPartitionWriter,
    RpcEndpointClient,
    RpcBlockHeaderSource,
    BlockHeaderSink,
]:
    """Stand up the full ingestion pipeline for one bounded range."""
    adapter = RpcAdapter(
        RpcConfig(
            endpoints=(RpcEndpoint(url="https://rpc.example/test", name="test_endpoint"),),
        ),
        transport=transport,
        sleeper=lambda _s: _noop(),
        rng=_seeded_random(),
        metrics=_zero_metrics(),
    )
    chain_id = ChainId(CHAIN_ID)
    pool_id = PoolId(POOL_ID_INT)
    manifest = ManifestStore(tmp_path / "manifest.sqlite")
    writer = RawPartitionWriter(tmp_path, manifest)
    client = RpcEndpointClient(
        adapter=adapter,
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=POOL_MANAGER.to_hex(),
    )
    header_source = RpcBlockHeaderSource(
        adapter=adapter,
        alias=ALIAS_ROBINHOOD_PUBLIC,
        batch_size=4,
    )
    header_sink = BlockHeaderSink(manifest=manifest, endpoint_alias=ALIAS_ROBINHOOD_PUBLIC)
    from _ingestion_t032_fixtures import (
        make_budget_snapshot,
        make_capability_snapshot,
    )

    cap = make_capability_snapshot(robinhood_max_blocks=planner_max_blocks, alchemy_max_blocks=10)
    bud = make_budget_snapshot(robinhood_calls=10, alchemy_calls=10)
    router_cfg = RouterConfig(
        failover_order=(ALIAS_ROBINHOOD_PUBLIC,),
        max_response_bytes=5_000_000,
        max_blocks_per_sub_range=planner_max_blocks,
        alchemy_max_blocks_per_get_logs=10,
    )
    runner = IngestionRunner(
        manifest=manifest,
        writer=writer,
        chain_id=chain_id,
        contract_address=POOL_MANAGER,
        pool_id=pool_id,
        pool_manager_address=POOL_MANAGER,
        pool_init_block=POOL_INIT_BLOCK,
        planner=RangePlanner(max_blocks_per_sub_range=planner_max_blocks),
        router_config=router_cfg,
        capability_snapshot=cap,
        budget_snapshot=bud,
    )
    runner.run_id = "test-e2e-t035-real"
    runner.register_client(ALIAS_ROBINHOOD_PUBLIC, client)
    runner.register_header_source(source=header_source, sink=header_sink)
    return runner, manifest, writer, client, header_source, header_sink


async def _noop() -> None:
    return None


def _seeded_random() -> Any:
    import random

    return random.Random(42)


def _zero_metrics() -> Any:
    from robinhood_lp.rpc.adapter import RpcMetrics

    return RpcMetrics()


# ---------------------------------------------------------------------------
# The end-to-end test
# ---------------------------------------------------------------------------


def test_e2e_t035_full_real_mainnet_path_persists_events_with_header_evidence(
    tmp_path: Path,
) -> None:
    """Run one bounded ingestion through the full T035 pipeline.

    The scripted adapter returns one ``Initialize`` row at block
    ``POOL_INIT_BLOCK + 7``. The scripted transport answers the
    ``eth_getBlockByNumber`` batch payload for that one block with a
    deterministic non-hydrated header.

    Acceptance:

    - ``result.complete`` is True (the planner's one sub-range is
      fully covered);
    - the runner issued exactly one logical ``eth_getLogs`` and one
      logical ``eth_getBlockByNumber``;
    - the on-disk Parquet partition exposes the persisted event
      with non-zero ``block_timestamp`` and non-zero ``parent_hash``;
    - the ``(block_number, block_hash, parent_hash, timestamp)``
      tuple the Parquet row carries equals the values the source
      returned;
    - the manifest ``block_headers`` table has exactly one row per
      distinct event block (here: one row);
    - no ``eth_call`` was issued during acquisition.
    """
    block_number = POOL_INIT_BLOCK + 7
    block_hash_int = 0xAABBCC
    parent_hash_int = 0x998877
    timestamp_int = 1_700_000_007
    transaction_hash_int = 0x123456

    # Pre-script the header the source's batch path will fetch.
    _HEADER_TABLE.clear()
    _HEADER_TABLE[block_number] = _make_header_payload(
        block_number=block_number,
        block_hash=block_hash_int,
        parent_hash=parent_hash_int,
        timestamp=timestamp_int,
    )
    transport = _ScriptedTransport()
    raw_row = _make_initialize_row(
        block_number=block_number,
        block_hash=block_hash_int,
        transaction_hash=transaction_hash_int,
        transaction_index=0,
        log_index=0,
    )
    transport.eth_get_logs_responses.append([raw_row])
    runner, manifest, writer, _client, header_source, header_sink = _build_runner(
        tmp_path=tmp_path, transport=transport
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is True, (
        f"expected complete=True; got halt_reason={result.halt_reason!r}, "
        f"interval_table={result.interval_table!r}"
    )
    # The interval table records one successful row carrying our
    # ``Initialize`` event.
    assert result.interval_count == 1
    row = result.interval_table[0]
    assert row["state"] == "successful"
    assert row["rows"] == 1
    # The source fetched exactly one logical ``eth_getBlockByNumber``
    # for the single distinct event block.
    assert header_source.metrics.logical_get_block_by_number_calls == 1
    assert header_source.metrics.http_batch_requests == 1
    assert header_source.metrics.header_cache_hits == 0
    # The dedup'd ``block_headers`` manifest table holds one row
    # for the single distinct event block (T035 acceptance: one
    # row per distinct event block).
    rows = manifest.count_block_headers(chain_id=CHAIN_ID)
    assert rows == 1
    header_row = manifest.get_block_header(chain_id=CHAIN_ID, block_hash=block_hash_int)
    assert header_row is not None
    assert header_row["block_number"] == block_number
    assert int(header_row["parent_hash"], 16) == parent_hash_int
    assert header_row["block_timestamp"] == timestamp_int
    assert header_row["endpoint_alias"] == ALIAS_ROBINHOOD_PUBLIC
    # The on-disk Parquet partition exposes the persisted event with
    # non-zero ``block_timestamp`` / ``parent_hash`` and the
    # ``(block_number, block_hash, parent_hash, timestamp)`` tuple
    # matches the source's response.
    import pyarrow.parquet as pq  # type: ignore[import-untyped]

    partition_paths = sorted((tmp_path / "raw").rglob("data.parquet"))
    assert len(partition_paths) == 1, partition_paths
    table = pq.read_table(str(partition_paths[0]))
    assert table.num_rows == 1
    row_index = 0
    persisted_block_timestamp = int(table.column("block_timestamp")[row_index].as_py())
    persisted_parent_hash = bytes(table.column("parent_hash")[row_index].as_py())
    persisted_block_hash = bytes(table.column("block_hash")[row_index].as_py())
    assert persisted_block_timestamp == timestamp_int
    assert int.from_bytes(persisted_parent_hash, "big") == parent_hash_int
    assert int.from_bytes(persisted_block_hash, "big") == block_hash_int
    assert int(table.column("block_number")[row_index].as_py()) == block_number
    # ``event_name`` matches the persisted event type.
    event_name = table.column("event_name")[row_index].as_py()
    assert event_name == "Initialize"
    # The dedup'd header count is exactly the number of distinct
    # event blocks (T035 acceptance: deduped header count == event
    # block count).
    header_distinct = len(manifest.list_block_headers(chain_id=CHAIN_ID))
    assert header_distinct == 1
    # The transport recorded the ``eth_getLogs`` and the
    # ``eth_getBlockByNumber`` calls and never issued ``eth_call``.
    assert transport.eth_call_invocations == 0
    methods_used = sorted(set(transport.used_methods))
    assert methods_used == ["eth_getBlockByNumber", "eth_getLogs"]


def test_e2e_t035_injected_header_fetch_failure_halts_run_non_complete(
    tmp_path: Path,
) -> None:
    """An injected header-fetch failure halts the run non-complete.

    The scripted transport replies with a successful ``eth_getLogs``
    row but returns no header for the requested batch. The runner
    must halt with ``complete=False`` and the explicit
    ``header_fetch_failed`` reason code (T035 acceptance: an
    injected header-fetch failure ends the run non-complete with an
    explicit reason code).
    """
    block_number = POOL_INIT_BLOCK + 7
    block_hash_int = 0xAABBCC

    _HEADER_TABLE.clear()
    # Intentionally do NOT script a header for ``block_number``;
    # the source's batch path will raise BlockHeaderFetchError when
    # the adapter call returns an empty payload.
    transport = _ScriptedTransport()
    raw_row = _make_initialize_row(
        block_number=block_number,
        block_hash=block_hash_int,
        transaction_hash=0x123456,
    )
    transport.eth_get_logs_responses.append([raw_row])
    runner, manifest, _writer, _client, _source, _sink = _build_runner(
        tmp_path=tmp_path, transport=transport
    )

    # Replace the scripted transport with one that returns a 500
    # for the header fetch so the source raises BlockHeaderFetchError.
    def _failing_transport(url: str, payload: dict[str, Any]) -> Awaitable[dict[str, Any]]:
        async def _run() -> dict[str, Any]:
            if "batch" in payload:
                raise RuntimeError("injected header fetch failure")
            method = payload.get("method")
            if method == "eth_getLogs":
                return {"jsonrpc": "2.0", "id": payload["id"], "result": [raw_row]}
            raise AssertionError(f"unexpected method {method!r}")

        return _run()

    # Rebuild the source / client around the failing transport so
    # the header fetch fails on the dedicated batch path. The
    # ``eth_getLogs`` call still succeeds.
    failing_adapter = RpcAdapter(
        RpcConfig(
            endpoints=(RpcEndpoint(url="https://rpc.example/test", name="test_endpoint"),),
        ),
        transport=_failing_transport,
        sleeper=lambda _s: _noop(),
        rng=_seeded_random(),
        metrics=_zero_metrics(),
    )
    from robinhood_lp.ingestion.block_header_source import BlockHeaderSink as _BHS
    from robinhood_lp.ingestion.endpoint_client import RpcEndpointClient as _REC

    failing_source = RpcBlockHeaderSource(
        adapter=failing_adapter,
        alias=ALIAS_ROBINHOOD_PUBLIC,
        batch_size=4,
    )
    failing_client = _REC(
        adapter=failing_adapter,
        alias=ALIAS_ROBINHOOD_PUBLIC,
        address=POOL_MANAGER.to_hex(),
    )
    runner.block_header_source = failing_source
    runner.header_cache = None  # reset cache from earlier wiring
    runner.register_header_source(
        source=failing_source,
        sink=_BHS(manifest=manifest, endpoint_alias=ALIAS_ROBINHOOD_PUBLIC),
    )
    runner._clients[ALIAS_ROBINHOOD_PUBLIC] = failing_client
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is False
    assert result.halt_reason == "header_fetch_failed"
    # The block_headers manifest table is empty (the failing run
    # did not persist any header).
    assert manifest.count_block_headers(chain_id=CHAIN_ID) == 0


def test_e2e_t035_deduped_headers_across_multiple_event_blocks(
    tmp_path: Path,
) -> None:
    """A range that contains multiple distinct event blocks yields
    exactly one header row per distinct block in the manifest table.

    The fixture returns three ``Initialize`` rows across two
    distinct blocks (one row per block plus a second row sharing the
    first block's number). The dedup invariant must hold: two
    distinct ``block_hash`` entries, each with one manifest row.
    """
    block_a = POOL_INIT_BLOCK + 5
    block_b = POOL_INIT_BLOCK + 9
    hash_a = 0x111
    hash_b = 0x222
    parent_a = 0x100
    parent_b = 0x111
    ts_a = 1_700_000_005
    ts_b = 1_700_000_009

    _HEADER_TABLE.clear()
    _HEADER_TABLE[block_a] = _make_header_payload(
        block_number=block_a,
        block_hash=hash_a,
        parent_hash=parent_a,
        timestamp=ts_a,
    )
    _HEADER_TABLE[block_b] = _make_header_payload(
        block_number=block_b,
        block_hash=hash_b,
        parent_hash=parent_b,
        timestamp=ts_b,
    )
    transport = _ScriptedTransport()
    rows = [
        _make_initialize_row(
            block_number=block_a,
            block_hash=hash_a,
            transaction_hash=0xA1,
            transaction_index=0,
            log_index=0,
        ),
        _make_initialize_row(
            block_number=block_a,
            block_hash=hash_a,
            transaction_hash=0xA2,
            transaction_index=1,
            log_index=1,
        ),
        _make_initialize_row(
            block_number=block_b,
            block_hash=hash_b,
            transaction_hash=0xB1,
            transaction_index=0,
            log_index=0,
        ),
    ]
    transport.eth_get_logs_responses.append(rows)
    runner, manifest, _writer, _client, header_source, _sink = _build_runner(
        tmp_path=tmp_path, transport=transport, planner_max_blocks=20
    )
    result = runner.run(
        requested_start_block=POOL_INIT_BLOCK,
        requested_end_block=POOL_INIT_BLOCK + 9,
    )
    assert result.complete is True
    # Two distinct event blocks -> two ``eth_getBlockByNumber`` calls
    # (no extra calls for the second row that shares block_a).
    assert header_source.metrics.logical_get_block_by_number_calls == 2
    # The manifest's ``block_headers`` table holds exactly one row
    # per distinct event block.
    assert manifest.count_block_headers(chain_id=CHAIN_ID) == 2
    block_headers = manifest.list_block_headers(chain_id=CHAIN_ID)
    observed_block_numbers = sorted(int(h["block_number"]) for h in block_headers)
    assert observed_block_numbers == [block_a, block_b]
    observed_parent_hashes = sorted(int(h["parent_hash"], 16) for h in block_headers)
    assert observed_parent_hashes == sorted([parent_a, parent_b])
    observed_timestamps = sorted(int(h["block_timestamp"]) for h in block_headers)
    assert observed_timestamps == sorted([ts_a, ts_b])
    # No ``eth_call`` during acquisition.
    assert transport.eth_call_invocations == 0
    # The Parquet partition row timestamps match the headers.
    import pyarrow.parquet as pq

    partition_paths = sorted((tmp_path / "raw").rglob("data.parquet"))
    table = pq.read_table(str(partition_paths[0]))
    persisted_timestamps = sorted(int(t) for t in table.column("block_timestamp").to_pylist())
    assert persisted_timestamps == sorted([ts_a, ts_a, ts_b])
