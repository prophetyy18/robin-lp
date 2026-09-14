# P07 — Central risk and paper execution

**Purpose:** run preliminary paper validation of the same intent/risk/ledger contracts
under live data without creating any ability to transact. This validates implementation,
not final live-promotion evidence.

**Entry:** robust backtest evidence and explicit authorization for preliminary paper on
the selected PoolKey.

**Exit gate:** fault/restart/shadow tests reconcile; all kill switches fail closed;
the process contains no signer/broadcaster.

**Phase prohibitions:** no wallet approvals, calldata broadcast, private keys,
real balances presented as paper balances, or risk bypass for manual intents.

## Tasks

- [T070 — Define centralized risk checks](T070.md)
- [T071 — Implement paper execution and immutable ledger](T071.md)
- [T072 — Add real-time read-only ingestion and recovery](T072.md)
