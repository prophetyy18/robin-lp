# Provider Facts — Robinhood Chain mainnet RPC endpoints

> Measured capability facts for the two qualified ingestion endpoints of ADR-010.
> These are **observations, not constants**: they are planning evidence and the
> fixed reference baseline for T035 / T036, and every ingestion run still
> re-probes endpoint capability at run start (ADR-010 decision 1, T032
> preflight probe). Recording a value here never authorizes assuming it at run
> time.

**Retrieval time:** 2026-09-17 (read-only probes; no signing or broadcast call)
**Chain:** Robinhood Chain mainnet, `chainId` 4663
**Observed head block at probe time:** 65392511
**Endpoint aliases:** A = `robinhood-official-mainnet` (Robinhood public mainnet
RPC), B = `alchemy-free-mainnet` (operator-supplied Alchemy Free Robinhood
mainnet endpoint). The aliases are the A/B pair from ADR-010 and ADR-011.

The 2026-09-16 identity, cross-provider comparison, and range-acceptance facts
for the same two endpoints are retained separately in
`docs/implement/protocol-artifacts/rpc-mainnet-free-ab-validation-2026-09-16.json`.
This document carries the 2026-09-17 result-bearing measurements.

Two later sections are not capability measurements and are labelled where they
appear: the Owner's finalized-pinning rule for the ten-million-block research
window, and the qualification record of a named run.

## Reference target used for the result-bearing measurements

- PoolId
  `0x6c614c38c65fea492f4cb2b90fd664f924a7b828c7384620662217e2e2df43ed`
- PoolKey: `currency0` `0x5fc5360d0400a0fd4f2af552add042d716f1d168` (USDG, 6
  decimals), `currency1` `0x7dbf38976f6d3b9c529e7d9484a71898b409ee6a` (ZZZ, 18
  decimals), `fee` 28001, `tickSpacing` 280, `hooks` `0x0`
- PoolManager `0x8366a39cc670b4001a1121b8f6a443a643e40951`, StateView
  `0xf3334192d15450cdd385c8b70e03f9a6bd9e673b`
- Reference range: blocks `54946237..55946237` inclusive (1,000,001 blocks,
  fully finalized history at probe time)

## Measured facts

1. **One pool-filtered range query covers the whole reference range.** The
   single `eth_getLogs` filter `[[5 topic0s], poolId]` served the entire range in
   one call and returned 3739 events across 3266 distinct blocks: Initialize 1,
   ModifyLiquidity 578, Swap 3159, ProtocolFeeUpdated 1, Donate 0.
2. **`eth_getLogs` `blockTimestamp` is unusable as a time source.** The field held
   `0x0` on every observed log. Block time must be read from
   `eth_getBlockByNumber`.
3. **A full-chain-range five-topic0 OR query times out.** The request failed with
   `-32000 log query timed out`.
4. **A single-event full-range query is rejected.** The request failed with
   `-32000 logs matched by query exceeds limit of 10000`.
5. **Rapid sequential calls produce HTTP 429.** Throttling is observed on
   back-to-back calls rather than only at extreme rates.
6. **B cannot carry the main scan.** B accepts about 10 blocks per `eth_getLogs`
   call, so it participates only in sampled cross-validation inside that measured
   capability; the wide pool-filtered backfill is A's job (ADR-010 decision 2/3).
7. **Both qualified endpoints support JSON-RPC batching of
   `eth_getBlockByNumber`.** Header retrieval can reduce HTTP round trips without
   changing the logical call count (ADR-011).
8. **Historical state availability differs by endpoint.** A cannot serve
   historical state through `eth_call` at the reference-range depth (about
   9.4–10.4 million blocks behind the observed head); B can. A block-pinned
   StateView read must therefore be issued to an endpoint able to serve
   historical state at that depth, and the operator runbook must state that
   routing explicitly.

## Consequences for the acquisition shape

- One pool-filtered range query per planned sub-range; events persisted with their
  `(block_number, transaction_index, log_index)` position and state rebuilt
  locally downstream.
- Never one `eth_getLogs` call per block, never one `eth_getBlockByNumber` call per
  scanned block, never a header for a block with no pool event, and no `eth_call`
  during acquisition.
- Header logical calls are deduplicated per distinct event block and issued as
  JSON-RPC batches; the logical-call versus HTTP-batch split is reported.
- The block time and parent hash of every event block come from
  `eth_getBlockByNumber`, and their persistence location is fixed by ADR-012.

## Limitations

- Every value above is a mutable observation of a third-party endpoint, not a
  contract constant. Re-probe before claiming a new range is complete
  (ADR-010 re-probe triggers).
- The result-bearing measurement covers one pool and one range. It does not
  establish unlimited retention, capacity, or rate limits for other pools,
  ranges, endpoints, or horizons.
- The reference-range event counts describe that finalized range only; they are
  not a density or volume constant for other ranges.
- No credential-bearing URL, API key, authorization header, or environment-file
  value is recorded in this document.

## Finalized pinning and the research window (Owner decision 2026-09-19)

**The per-pool rule below replaces the ten-million-block rule recorded in the
following subsection.** The older text is retained as history: it describes the rule
T038 implemented and the datasets that rule produced, and no clause of it may be cited
as the research range. The replacement is recorded in `ADR-015`.

- **[Owner policy, 2026-09-19]** Each pool in the research universe is acquired over
  its own inclusive window from **that pool's `Initialize` block** to the run's agreed
  finalized block. There is no fixed width, no extension allowance, and no
  `pool_init_outside_window` exclusion: a pool whose `Initialize` block cannot be
  located inside the candidate range is a failed resolution that stops the run and is
  reported. One pool's window is never widened to reach another pool's initialization.
- **[Measured observation, 2026-09-19 — re-probe every run]** The block interval is
  **0.1008 seconds**, derived from this project's own persisted block headers (3,266
  headers spanning 999,704 blocks and 100,819 seconds). A ten-million-block window is
  therefore about **11.7 days**, and the roughly 65 million blocks observed at the last
  probe are about **76 days** of chain history. These are observations of a mutable
  chain, not constants.
- **[Measured observation, 2026-09-19]** Cost scales with the pool's age rather than
  with a fixed width: a single pool-filtered `eth_getLogs` call covered the entire
  one-million-block reference range, and 3,266 distinct event blocks required only
  deduplicated, batched header calls. The per-call log ceiling, not the range width,
  is what forces a scan to be split.
- **[Run-specific measurement — re-probe every run]** The `finalized` height, its
  hash, each endpoint's finality-tag support, archive-state depth and accepted log
  range remain mutable third-party observations. Every run re-probes at its own start
  and records its own values, and a declared budget ceiling halts the run rather than
  being exceeded silently.

The remainder of this section is the superseded 2026-09-18 record.

### Superseded: the ten-million-block rule (Owner decision 2026-09-18)

The ten-million-block research window supersedes the 1,000,001-block reference range
above. Its values are not all the same kind of fact, so each is labelled. Provenance:
measured values here come from the same 2026-09-17 read-only probe, chain id 4663,
endpoint aliases A and B, as the facts above; policy values come from the Owner
decision of 2026-09-18 and are not measurements.

- **[Owner policy, 2026-09-18]** The window end is the `finalized` block read at run
  start from both qualified endpoints (aliases A and B above). The two readings must
  agree on block number **and** block hash; that agreed block is the window end,
  pinned by number and hash into the run record. `latest`, a non-finalized block and
  any wall-clock-derived bound are excluded as the window end, and there is no
  fallback to `latest` when the tag is unavailable.
- **[Owner policy, 2026-09-18]** The window start is
  `min(end - 10_000_000, Initialize block of each included pool)`. The start is
  extended below `end - 10_000_000` only when a pool's `Initialize` lies within
  2,000,000 blocks of that bound; a pool whose `Initialize` lies further below is
  reported `pool_init_outside_window` and excluded from the qualified dataset rather
  than widening the window.
- **[Chain fact, reference pool record]** The pinned reference pool's `Initialize`
  block is `54946237` on chain id 4663 (the reference range above starts at that
  block). It identifies that pool's initialization; it is not a rule, and an
  acquisition still has to find the pool's own `Initialize` evidence inside the range
  it acquires.
- **[Run-specific measurement — re-probe every run]** The `finalized` height and its
  hash, each endpoint's finality-tag support, archive-state depth and accepted log
  range are mutable third-party observations. No finalized height is recorded here,
  because a number measured today is not evidence for a later run; every run
  re-probes at run start (ADR-010 decision 1) and records its own values.

The second pool of the window is Owner-pinned by hook contract address
`0xEd50bDeeA8aDC232f159486192a4157281D722ff` on chain id 4663. The pinned value
is a **lookup signal** (the `hooks` field of the second pool's `PoolKey`); it is
not a V4 `PoolId` and is not a keccak256 digest. Its `PoolKey` (`currency0`,
`currency1`, `fee`, `tickSpacing`) and its 32-byte `PoolId` (the keccak256
digest emitted on chain) and its `Initialize` block are **not** recorded here:
they are unresolved and are resolved on chain by T038 by scanning `Initialize`
logs whose decoded `hooks` field equals the pinned hook contract address. T038
then verifies `keccak256(abi.encode(PoolKey)) == PoolId`, where `PoolId` is the
32-byte keccak256 digest the chain emits (not the pinned 20-byte hook address).
No value for those fields may be assumed from this document.

## Dataset qualification records

### `run-680e65f4a59842d98b1712a45280779d` (2026-09-18) — superseded, non-qualifying

This run's dataset does **not** satisfy the Phase 3 exit gate and is **not** a Phase 4
input. It must not be repaired, re-qualified, overwritten or merged, and no Phase 4
contract may cite it as a satisfied entry condition. It remains immutable historical
evidence for the defect below.

- The two layers disagree: `event_index` holds 3,739 rows across 3,266 distinct event
  blocks while the Parquet partitions hold 3,737 rows across 3,264, and the run's own
  per-event-type comparison fails closed (`Swap` 3,157 against its pinned baseline
  3,159).
- The two missing rows are `Swap` events at block `55586273` (log index 53,
  transaction
  `0x17c7771d139bbf8f8cb7d5b7b1cd0ccb09ca6aefa43b6e36df400e26fb5e936e`) and block
  `55726297` (log index 29, transaction
  `0xd75437f4a29d3763edf90eebc8cfd179f63b2f278f01d9b9663ac2ab0a5d79d4`). The
  canonical `EventKey` of each row adds its block's hash, so a re-check compares the
  `EventKey` sets rather than row counts alone.
- The cause is a storage-writer defect, not a chain event:
  `RawPartitionWriter._merge_into_existing_partition`
  (`src/robinhood_lp/storage/writer.py`) appended those rows to `event_index` and
  deliberately did not modify the Parquet file, and the run reported `complete=1`
  with an empty `reorg_journal`, `conflicting_observations` and
  `run_manifest_deviations`. Closing the defect and delivering the per-partition
  `event_index`-versus-Parquet reconciliation are owned by T037.

## Sources

R23, R24, R25, and read-only runtime probes of the configured endpoints made
with this project's own JSON-RPC transport (ADR-011). Runtime probes remain
authoritative for the configured endpoint; the published documentation describes
the plan, not the observed endpoint.
