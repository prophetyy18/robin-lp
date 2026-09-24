# P06 — Event-driven backtesting and research protocol

**Purpose:** evaluate generic strategies under explicit information, fill, latency,
cost, and robustness assumptions, and expose that evaluation through one product-level
application API that names the registered strategy, its parameters and its data range.

**Entry:** Phase 5 exit gate and target pools at `backtest` support.

**Exit gate:** manifests reproduce decisions/ledger/reports; look-ahead adversarial
tests pass; results include honest USDG cash/HODL/rebalancing benchmarks and full costs;
T068's registry is the identity source for every strategy a run can execute; T105–T108
are the designated registry-bound successors for the manifest, robustness,
candidate-lock and authoring-guide paths; T112 is approved as the current registry-bound
manifest and evidence authority; T110 qualifies exact historical market/run state while
composing rather than replacing T104; T111 migrates the approved research consumers; and
T113 exposes the stable backtest application contract and completes the T112-only CLI
composition. Product publication remains disabled until that cutover passes. T107 produces an
explicitly provisional candidate marked `EXPERIMENTAL_NOT_LIVE_APPROVED`, or an honest
`NO_TRADE`. Neither is live approval.

**Phase prohibitions:** no strategy RPC/storage access, same-event clairvoyant fill,
parameter selection on held-out data, or profitability-based acceptance.

## Tasks

- [T060 — Define strategy contracts](T060.md)
- [T061 — Build the event-driven backtest engine](T061.md)
- [T062 — Implement baseline strategies](T062.md)
- [T063 — Add experiment manifests and reports](T063.md) — predecessor delivery; T105 is the designated current successor after retirement
- [T064 — Add robustness and anti-overfitting analysis](T064.md) — predecessor delivery; T106 is the designated current successor after retirement
- [T065 — Implement the maintainable USDG-first adaptive-Range strategy](T065.md)
- [T066 — Build versioned threshold experiments and provisional candidate review](T066.md) — predecessor delivery; T107 is the designated current successor after retirement
- [T067 — Write the strategy authoring guide](T067.md) — predecessor delivery; T108 is the designated current successor after retirement
- [T068 — Register strategy identities with their parameter schema and code provenance](T068.md)
- [T069 — Deliver the product-level backtest run entry point](T069.md) — predecessor delivery; T109 is the designated current successor after retirement
- [T105 — Bind experiment manifests and reports to the strategy registry](T105.md) — predecessor delivery; T109 is the designated current successor after retirement
- [T106 — Constrain robustness analysis to registered parameter schemas](T106.md)
- [T107 — Lock candidate identity, schema and registry revision](T107.md)
- [T108 — Update the strategy authoring guide for registered identity](T108.md)
- [T109 — Publish causally bound strategy-run evidence](T109.md)
- [T110 — Qualify exact historical market and run-state replay](T110.md)
- [T111 — Cut approved research consumers over to replay artifacts](T111.md)
- [T112 — Repair product backtest entry to bind real partitions and fill-cursor run facts](T112.md)
- [T113 — Expose the backtest application contract and wire the approved T112 path](T113.md)
