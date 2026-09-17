---
id: ADR-012
title: Block-header time persistence and acquisition call volume
status: accepted
date: 2026-09-17
owner: Owner / Phase 3 (A0001)
supersedes: []
references: [R9, R23, R24, ADR-002, ADR-010, ADR-011, T035, T036]
---

# ADR-012 — Block-header time persistence and acquisition call volume

## Context

This ADR is an **addendum**. It refines ADR-002 (storage and query format) and
ADR-010 (free dual-provider historical ingestion) with measured facts; it
reopens neither. ADR-002 already fixes a two-layer Parquet + SQLite layout, and
ADR-010 decision 9 already requires one non-hydrated block header per distinct
event block. Two things were still under-specified:

- **where** block time and parent hash live, and
- **how much** acquisition traffic the range scan is allowed to produce.

A read-only probe on 2026-09-17 against the public mainnet endpoint (chain id
4663, observed head block 65392511) settled both, and the detailed values are
recorded in `docs/spec/protocol/PROVIDER_FACTS.md`:

- one pool-filtered `eth_getLogs` call with the five-topic0 OR filter served a
  1,000,001-block reference range: 3739 events across 3266 distinct blocks;
- the `eth_getLogs` `blockTimestamp` field was `0x0` on every observed log;
- a full-chain-range five-topic0 OR query timed out (`-32000 log query timed
  out`) and a single-event full-range query was rejected (`-32000 logs matched by
  query exceeds limit of 10000`);
- rapid sequential calls produced HTTP 429;
- the second qualified endpoint accepts about 10 blocks per `eth_getLogs` call
  and therefore cannot carry the main scan;
- both qualified endpoints support JSON-RPC batching of `eth_getBlockByNumber`;
- the primary endpoint cannot serve historical state through `eth_call` at that
  depth while the second endpoint can.

The event stream is sparse relative to the scanned range, so any shape that pays
per-block cost is both contract-forbidden and measurably unusable.

## Decision

1. **Header/time persistence location.** Block time and parent hash are carried
   by three required parts, together and never as alternatives:
   - the T030 logical header record keyed by `(chain_id, block_hash)` carrying
     `block_number`, `parent_hash` and `timestamp`;
   - the T031 Parquet partition columns `block_timestamp` (uint64) and
     `parent_hash` (binary(32)), which serve event-time ordering and replay;
   - the T031 manifest header table holding exactly one deduplicated row per
     distinct event block, which serves dedup, coverage accounting and
     cross-checking.

   The time source is the non-hydrated `eth_getBlockByNumber` header.
   `eth_getLogs.blockTimestamp` is rejected as a time source because it was
   measured as `0x0`, and no wall-clock value may substitute for block time. A
   header that cannot be obtained halts the interval; it is never defaulted to
   zero or to the request time.

2. **Acquisition call volume.** The acquisition shape is fixed:
   - one pool-filtered range query per planned sub-range, with events persisted
     with their `(block_number, transaction_index, log_index)` position and state
     rebuilt locally downstream;
   - never one `eth_getLogs` call per block, never one `eth_getBlockByNumber` call
     per scanned block, never a header for a block with no pool event, and no
     `eth_call` during acquisition at all;
   - header logical calls are deduplicated per distinct event block and issued as
     JSON-RPC batches, with the logical-call versus HTTP-batch split reported as
     separate counters (ADR-011: a batch never counts as fewer logical calls).

3. **Endpoint roles follow measured capability.** The primary endpoint carries the
   main scan; the second endpoint participates within its own measured capability
   only, so its about-10-block log range is used for sampled cross-validation and
   for the block-pinned state reads the primary cannot serve at depth. The
   operator runbook states that routing explicitly.

## Consequences

Block time becomes reproducible from chain evidence alone: any event's timestamp
can be re-checked against the header of its own block hash, and the `(chain_id,
block_hash)` key makes a same-height fork a distinct header rather than a
silently overwritten row. Cost scales with distinct event blocks rather than with
scanned blocks, which is what makes a sparse-range bootstrap affordable on the
free path. The three carriers add a consistency obligation: a run that persists
only one of them is incomplete, not partially correct.

This ADR does not change ADR-002's storage engine or ADR-010's routing,
provenance, or sampled-agreement rules; it fixes the header/time carrier set and
the call-volume shape those rules operate on. Storage-engine changes and routing
changes remain governed by ADR-002 and ADR-010 respectively.

## Re-probe triggers

Re-measure the facts in `docs/spec/protocol/PROVIDER_FACTS.md` and revisit this
decision when the provider plan, endpoint, headers, chain deployment, requested
horizon, event density, or an observed limit changes; before claiming any new
range is complete; and whenever a run observes a capability regression against
the recorded values.

## Owner

Owner / Phase 3 (Owner amendment A0001). Implementation of the two clauses above
is owned by T035; the reference-dataset qualification that proves the call-volume
bound on a real range is owned by T036.
