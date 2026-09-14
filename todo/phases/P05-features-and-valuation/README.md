# P05 — Point-in-time features, position valuation, and attribution

**Purpose:** turn reconciled protocol state into decision-safe features and fully
reconciled economic results.

**Entry:** Phase 4 exit gate for each target pool, including T043 for every hooked
pool promoted beyond ingestion.

**Exit gate:** every feature has availability time/units; position accounting closes
for boundary scenarios; quote valuation has point-in-time provenance.

**Phase prohibitions:** no backward fill, centered windows, future close prices,
unlabeled oracle substitution, or residual PnL hidden in “other”.

## Tasks

- [T049 — Specify and implement V4 liquidityDelta sizing in USDG terms](T049.md)
- [T050 — Add time/block bars and market features](T050.md)
- [T051 — Add LP position valuation](T051.md)
- [T052 — Add benchmarks and PnL attribution](T052.md)
- [T053 — Add point-in-time quote and gas valuation](T053.md)
