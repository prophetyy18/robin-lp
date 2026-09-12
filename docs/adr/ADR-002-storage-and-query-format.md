---
id: ADR-002
title: Storage and query format
status: proposed
date: 2026-09-12
owner: T000
supersedes: []
references: [R4, R9, R13]
---

# ADR-002 — Storage and query format

## Context

Historical V4 data must be preserved immutably enough that:

- raw logs survive re-decoding against future ABI / schema versions;
- interrupted writes recover without partial partitions;
- reorg handling can mark orphaned rows without destroying audit history;
- downstream replay/feature/backtest code reads only explicitly qualified
  datasets (range, schema version, manifest checksum).

Storage must support append-only ingestion, idempotent overlap, and
machine-readable coverage reports. It must not depend on a running
service during deterministic replay.

## Decision

Adopt a **two-layer local storage layout**:

1. **Append-only raw partitions** partitioned by
   `(chain_id, contract_address, event_name, block_range)` using
   **Parquet** files (columnar, typed, schema-versioned). Each row
   preserves raw topics/data plus the JSON-RPC response wrapper, and a
   schema/decode version alongside normalized typed fields.
2. **Manifest + checkpoint store** as a single **SQLite** database file
   keyed by content hash. It records:
   - partition manifests (row count, block bounds, block hashes,
     SHA-256 of the file);
   - ingestion checkpoints (chain, contract, last successful block);
   - dataset manifests (range, schema version, source retrieval time,
     checksum);
   - reorg journal (orphan blocks, replacement blocks, demotion
     reasons).

Parquet is preferred over row-oriented formats because:

- typed columns preserve integer widths without lossy coercion;
- column pruning and predicate pushdown keep replay scans tractable;
- it round-trips through the standard Python data tooling without
  requiring a running service.

SQLite is preferred over a separate database service because:

- it ships with Python, requires no external process, and is durable as
  a single file;
- it is sufficient for manifest/checkpoint volume at the scale this
  project targets;
- it is auditable as a single artifact per environment.

Both files live under a configured `data/` root. Raw partitions are
write-once per `(partition_id, version)`; manifests are append-only and
checksummed.

## Alternatives considered

- **Single SQLite for raw + manifest.** Simpler tooling but row-oriented
  storage inflates historical scans and makes column-typed schema
  evolution fragile.
- **Postgres / DuckDB server.** Adds an external dependency, complicates
  reproducibility, and offers no advantage at the current data volume.
- **LevelDB / RocksDB.** Fast but opaque on disk; harder to audit and
  to checksum meaningfully across versions.
- **JSONL only.** Human-readable but loses typed column widths, is
  larger on disk, and slows predicate-pushed scans.

## Consequences

Positive:

- append-only partitions and SHA-256 manifests give cheap tamper
  detection and reproducible datasets;
- SQLite as manifest store survives process restart with no extra
  service;
- schema-version columns in Parquet enable forward-compatible decode.

Negative / risks:

- Parquet files are not human-readable; debugging requires the
  project's reader or external tooling;
- SQLite single-writer model can become a bottleneck under heavy
  parallel ingestion; serialized manifest writes are an explicit
  boundary, not a surprise;
- on-disk layout choices (partition sizing, retention) need evidence
  from real ingestion volumes and are revisited in T031.

## Migration trigger

Re-evaluate this ADR if any of the following occur:

- ingestion volume exceeds what a single SQLite manifest store can
  serve without degrading replay startup time;
- a maintained, dependency-free columnar format with stronger
  append-only semantics becomes available;
- the project adopts a service-style deployment (e.g. distributed
  workers) that materially changes the access pattern.

## Owner

T000 (initial). Hand off to whoever owns the storage module introduced
by T031.
