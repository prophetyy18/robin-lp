---
id: ADR-015
title: Per-pool extended-history research window
status: accepted
date: 2026-09-19
owner: Owner (direct Owner edit, 2026-09-19)
supersedes: []
replaces_clause: ADR-013 window rule (Decision 1 and its 2,000,000-block extension allowance)
references: [ADR-002, ADR-010, ADR-011, ADR-012, ADR-013, T034, T035, T036, T038, T039]
---

# ADR-015 — Per-pool extended-history research window

## Context

This ADR is an **addendum** that replaces one decision inside ADR-013. It refines
ADR-002, ADR-010, ADR-011 and ADR-012 without reopening them, and it does not touch
ADR-013's partition-reconciliation clause, which stands unchanged. ADR-013 is
immutable once accepted, so its window rule is replaced here by reference rather than
edited in place.

ADR-013 fixed the research window at `min(end - 10_000_000, Initialize block)` with a
2,000,000-block extension allowance and a `pool_init_outside_window` exclusion for
pools initialized further below. That rule was written before the chain's scale was
measured. It is now measured, from this project's own persisted block headers:

- the observed block interval is **0.1008 seconds**, derived from 3,266 stored headers
  spanning 999,704 blocks and 100,819 seconds;
- so a ten-million-block window is **about 11.7 days**, while Robinhood Chain mainnet
  holds roughly 65 million blocks, or **about 76 days** of history in total;
- the pinned reference pool was initialized about 12 days before the last probe, which
  means the ten-million-block rule was, for that pool, very close to the whole of the
  history it has ever had — the fixed width was not the binding constraint there, and
  for a younger pool it is not either. For an older pool it discards history that
  exists and that was paid for once.

The cost side is equally measured. A single pool-filtered `eth_getLogs` call covered
the entire one-million-block reference range; 3,266 distinct event blocks required
only deduplicated, batched `eth_getBlockByNumber` calls; and the per-call log ceiling,
not the range width, is what forces a scan to be split. Widening the window by an
order of magnitude is therefore bounded and affordable, and it is the difference
between a dataset that can support a model and one that cannot: at the measured
interval, twelve days of one pool is roughly 3.4 thousand five-minute bars.

## Decision

**1. The window is per pool, not shared.** Each pool in the research universe is
acquired over its own inclusive range from **that pool's `Initialize` block** to the
run's agreed finalized block. One pool's window is never widened to reach another
pool's initialization, and no pool's data is attributed to another.

**2. The end is an agreed finalized block, pinned by number and hash.** The finalized
block is read from both qualified endpoints at run start and the two readings must
agree on block number **and** block hash. `latest`, a non-finalized block and any
wall-clock-derived bound are excluded as the end, including as a fallback when the
finalized tag is unavailable. A disagreement halts the run.

**3. Per-pool data roots, coverage reports and cost records.** Each pool carries its
own data root, its own machine-readable coverage report satisfying the existing
report requirements, and its own cost record separating logical calls, HTTP requests,
bytes, rows, provider units and elapsed time. A declared budget ceiling halts the run
rather than being exceeded silently.

**4. The acquisition shape is unchanged.** One pool-filtered range query per planned
sub-range, split under the measured per-call log ceiling; events persisted with their
`(block_number, transaction_index, log_index)` position; headers deduplicated per
distinct event block and issued as JSON-RPC batches; no `eth_call` during acquisition;
state rebuilt locally downstream. ADR-012's persistence location for block time and
parent hash still governs.

**5. The fixed-width rule is retired.** The `min(end - 10_000_000, Initialize)` start,
the 2,000,000-block extension allowance, and the `pool_init_outside_window` exclusion
no longer define the research range. A pool whose `Initialize` block cannot be located
inside the candidate range is a **failed resolution that stops and is reported**, not
a pool to be excluded from an otherwise valid dataset. T038's implementation, tests
and dataset generations remain immutable historical evidence: they are not edited,
re-qualified, overwritten or merged, and no later clause may cite the
ten-million-block window as the research range.

## Consequences

T039 implements this rule and supersedes T038's window decision. The Phase 4 texts
that treated `pool_init_outside_window` as a routine exclusion no longer describe a
reachable case and are corrected by owner amendment rather than silently ignored.
`docs/spec/protocol/PROVIDER_FACTS.md` records the provenance of every value used
here and labels measured observations separately from policy, and every ingestion run
still re-probes endpoint capability and the finalized height at its own start: the
numbers above justify the decision, they never authorize assuming a value at run
time. Cost grows with the pool's age rather than with a fixed width, so an older pool
costs more than a younger one and the budget ceiling is per pool, not global.

## Re-probe triggers

Re-measure and revisit when the observed block interval changes materially, when an
endpoint's per-call log ceiling or historical-state depth changes, when a pool's
`Initialize` block cannot be located, when a finalized disagreement is observed, or
when a run's cost record approaches its declared ceiling.

## Owner

Owner, by direct Owner edit on 2026-09-19. Implementation is owned by T039, which
supersedes T038's window rule; T034 owns the per-pool coverage report the run must
satisfy, and T035 and T036 own the live ingestion and qualification path this ADR
extends. This ADR changes no evidence recorded against an approved task.
