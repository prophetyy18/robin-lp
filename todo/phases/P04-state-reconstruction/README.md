# P04 — Deterministic state reconstruction and hook evidence

**Purpose:** prove what can and cannot be reconstructed from observations without
querying future state.

**Entry:** a complete, finalized dataset manifest from Phase 3.

**Exit gate:** supported pools reconcile at multiple pinned blocks; discrepancies
and hook assumptions are zero or explicitly blocking.

**Phase prohibitions:** no current/latest state in historical replay, no arbitrary
tolerance for exact integers, no inferred hook effects, no backtest on failed pools.

## Tasks

- [T040 — Build deterministic event replay](T040.md)
- [T041 — Reconstruct tick-liquidity state](T041.md)
- [T042 — Validate replay against independent chain state](T042.md)
- [T043 — Build hook semantics evidence packs](T043.md)
