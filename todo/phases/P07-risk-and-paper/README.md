# P07 — Central risk and paper execution

**Purpose:** run preliminary paper validation of the same intent/risk/ledger contracts
under live data without creating any ability to transact. This validates implementation,
not final live-promotion evidence.

**Entry:** robust backtest evidence, and explicit authorization for preliminary paper on
the selected PoolKey. The authorization half is completed by the Owner — the active-`PoolKey`
selection, the two tokens' `HOLD`/`LP` approvals and the preliminary-paper authorization,
each as an immutable versioned record with its audit event — through the pre-paper
authorization entry T073 delivers. T073 is therefore the first task activated in this
phase: T070 may start once the backtest evidence exists, while T071 and T072 must not
operate a paper run before T073 is `APPROVED`. No CLI, background process or later console
task performs those three journeys in the Owner's place.

**Exit gate:** fault/restart/shadow tests reconcile; all kill switches fail closed;
the process contains no signer/broadcaster.

**Phase prohibitions:** no wallet approvals, calldata broadcast, private keys,
real balances presented as paper balances, or risk bypass for manual intents.

## Tasks

- [T070 — Define centralized risk checks](T070.md)
- [T071 — Implement paper execution and immutable ledger](T071.md)
- [T072 — Add real-time read-only ingestion and recovery](T072.md)
- [T073 — Deliver the pre-paper authorization entry](T073.md)
