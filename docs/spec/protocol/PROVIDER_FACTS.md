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

## Sources

R23, R24, R25, and read-only runtime probes of the configured endpoints made
with this project's own JSON-RPC transport (ADR-011). Runtime probes remain
authoritative for the configured endpoint; the published documentation describes
the plan, not the observed endpoint.
