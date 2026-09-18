---
id: ADR-013
title: Finalized window pinning and partition reconciliation
status: accepted
date: 2026-09-18
owner: Owner / Phase 3-4 (A0002)
supersedes: []
references: [R9, R23, R24, R25, ADR-002, ADR-010, ADR-011, ADR-012, T037, T038]
---

# ADR-013 — Finalized window pinning and partition reconciliation

## Context

This ADR is an **addendum**. It refines ADR-002 (storage and query format),
ADR-010 (free dual-provider historical ingestion) and ADR-012 (header/time
persistence and acquisition call volume), and it relies on ADR-011 (bounded
JSON-RPC transport). It reopens none of them: ADR-011's transport and logical-call
accounting rules are unchanged, and no decision recorded here replaces a decision
recorded there.

Two facts made these rules necessary.

**A partition write could lose rows silently.** The 2026-09-18 reference run
(`run-680e65f4a59842d98b1712a45280779d`) did not satisfy the Phase 3 exit gate. Its
`event_index` recorded 3,739 rows across 3,266 distinct event blocks while its
Parquet partitions held 3,737 rows across 3,264, so its own per-event-type
comparison failed closed (`Swap` 3,157 against the pinned baseline 3,159). The cause
was not a chain event: when a collection interval wrote rows into a partition cell
an earlier interval had already created,
`RawPartitionWriter._merge_into_existing_partition`
(`src/robinhood_lp/storage/writer.py`) appended them to `event_index` and
deliberately did not modify the Parquet file, and the run reported `complete=1` with
an empty `reorg_journal`, `conflicting_observations` and `run_manifest_deviations`.
A dataset can therefore be reported complete while its index and its files disagree,
and totals alone do not reveal it.

**A named range whose end can still advance is not reproducible.** Two runs over the
same named range at different times cover different blocks, so coverage, row counts
and any downstream comparison drift with the run date rather than with the chain.
The research input accordingly moves to a wider window, and its end has to be fixed
by evidence rather than by a calendar.

The superseded dataset stays immutable historical evidence and is recorded, with the
two missing keys and the cause, in `docs/spec/protocol/PROVIDER_FACTS.md`.

## Decision

1. **Finalized window pinning.** The window end is the `finalized` block read at run
   start from both qualified endpoints; the two readings must agree on block number
   and block hash, and that agreed block is pinned by number and hash into the run
   manifest, the quality report and the evidence. `latest`, a non-finalized block
   and any wall-clock-derived bound are excluded as the window end, including as a
   fallback when the tag is unavailable. The window start is
   `min(end - 10_000_000, Initialize block of each included pool)`, extended below
   that bound only when a pool's `Initialize` lies within 2,000,000 blocks of it; a
   pool whose `Initialize` lies further below is reported as
   `pool_init_outside_window` and excluded from the qualified dataset instead of
   widening the window. Each included pool has its own data root and its own
   machine-readable completeness report.
2. **Partition reconciliation.** A partition whose `event_index` EventKeys are not
   present in its on-disk Parquet data is a failed write, not a successful one: the
   write path either stores those rows or fails closed with a reason code, and
   appending index rows without storing the corresponding Parquet rows is not an
   available outcome. A per-partition reconciliation compares the `event_index`
   EventKey set and row count against the Parquet file and is surfaced through the
   data-quality report, so `complete=true` cannot be produced while any partition
   disagrees. Rewriting committed Parquet bytes to reconcile a mismatch is not an
   available remedy; a repair path that edits a written partition file is out of
   scope, as is re-qualifying the superseded dataset.

Both rules are capability-independent: they hold for any endpoint, any pool and any
range width, and neither is a substitute for the per-run capability probe ADR-010
decision 1 already requires.

## Consequences

A dataset's coverage becomes a property of pinned chain evidence rather than of the
run date: the same pinned window can be re-acquired and compared block-for-block,
and a re-run that produces different coverage is visible as a discrepancy instead of
as a stale range. The end-to-end guarantee now has two layers of defence: the write
path fails closed instead of losing rows silently, and the reconciliation check
refuses completeness across a disagreement that already exists on disk. Cost and
coverage of a window stay reviewable because the pinned end, the per-pool data roots
and the per-partition reconciliation result are all recorded.

The obligations this creates are: an acquisition that cannot read a `finalized` tag
from both endpoints acquires nothing; an included pool must carry its own
`Initialize` evidence inside the window; and a partition-level disagreement is a
blocking finding, never a rounding or reporting detail.

This ADR does not change ADR-002's storage engine, ADR-010's routing, provenance or
sampled-agreement rules, ADR-011's call accounting, or ADR-012's header/time carriers
and call-volume shape; it fixes the window boundary those rules operate on and the
per-partition check that guards the files they write.

## Re-probe triggers

Re-measure `docs/spec/protocol/PROVIDER_FACTS.md` and revisit this decision when the
provider plan, endpoint, headers, chain deployment, requested horizon, event density,
or an observed limit changes; before claiming any new range or window is complete;
and whenever a run observes a capability regression, a `finalized` disagreement, or a
partition whose index and Parquet disagree.

## Owner

Owner / Phase 3-4 (Owner amendment A0002, 2026-09-18). Implementation of the two
clauses above is owned by T037 (partition guard and reconciliation) and T038 (pinned
ten-million-block acquisition); the Phase 4 contracts T040 through T043 consume the
per-pool qualified datasets and re-implement neither clause.
