# P06 — Event-driven backtesting and research protocol

**Purpose:** evaluate generic strategies under explicit information, fill, latency,
cost, and robustness assumptions, and expose that evaluation through one product-level
run entry point that names the registered strategy, its parameters and its data range.

**Entry:** Phase 5 exit gate and target pools at `backtest` support.

**Exit gate:** manifests reproduce decisions/ledger/reports; look-ahead adversarial
tests pass; results include honest USDG cash/HODL/rebalancing benchmarks and full costs;
T068's registry is the identity source for every strategy a run can execute; T105–T108
are the designated registry-bound successors for the manifest, robustness,
candidate-lock and authoring-guide paths; and T069's
product-level run entry point turns a registered strategy, its parameters and a data range
into a reproducible manifest and report with observable run state; T109 adds causally bound,
dataset-addressed run evidence and atomic current publication, T110 qualifies exact historical
market/run state and replay frames while composing rather than replacing T104, and T111 cuts
the approved research consumers over to that current artifact chain; T112, the A0026
successor repair task, closes the three defects A0026 confirmed in the code paths that
produce new T109 artifacts — routing the backtest entry through real T100 partitions
via T040/T041 replay and T050 point-in-time features, binding the manifest to real
T100 partition refs and recording separately versioned `SimulationEvidence` run
facts sufficient to recover positions, integer inventory, equity, drawdown and T052
attribution at the actual fill cursor — without mutating T109's contract or its
historical approved evidence. T112 approval precedes reviewed consumer CONTRACT
amendments and T109 SUPERSEDE; current production publication resumes only after
an atomic T112-only cutover. T107 produces an
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
