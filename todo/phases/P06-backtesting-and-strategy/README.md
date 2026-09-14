# P06 — Event-driven backtesting and research protocol

**Purpose:** evaluate generic strategies under explicit information, fill, latency,
cost, and robustness assumptions.

**Entry:** Phase 5 exit gate and target pools at `backtest` support.

**Exit gate:** manifests reproduce decisions/ledger/reports; look-ahead adversarial
tests pass; results include honest USDG cash/HODL/rebalancing benchmarks and full costs;
T066 produces an explicitly provisional candidate marked
`EXPERIMENTAL_NOT_LIVE_APPROVED`, or an honest `NO_TRADE`. Neither is live approval.

**Phase prohibitions:** no strategy RPC/storage access, same-event clairvoyant fill,
parameter selection on held-out data, or profitability-based acceptance.

## Tasks

- [T060 — Define strategy contracts](T060.md)
- [T061 — Build the event-driven backtest engine](T061.md)
- [T062 — Implement baseline strategies](T062.md)
- [T063 — Add experiment manifests and reports](T063.md)
- [T064 — Add robustness and anti-overfitting analysis](T064.md)
- [T065 — Implement the maintainable USDG-first adaptive-Range strategy](T065.md)
- [T066 — Build versioned threshold experiments and provisional candidate review](T066.md)
