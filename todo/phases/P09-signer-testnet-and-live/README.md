# P09 — Isolated signer, testnet proof and gated mainnet execution

**Purpose:** deliver the V1 mainnet capability through isolated, narrowly authorized and
fully reconciled execution. Entering this phase does not itself authorize a transaction.

**Entry:** T083 and T086 evidence pass; Phase 8 contains no signing/broadcast path; the user
explicitly authorizes the specific Phase 9 implementation task that introduces such capability.

**Exit gate:** a smallest-approved-capital mainnet LP lifecycle completes under T094 scope;
every intent, risk decision, simulation, signature, transaction, receipt, balance and position
reconciles; T096 maps every V1 goal to final evidence.

**Phase prohibitions:** no key/password in Web/config/env/CLI args/logs/database; no arbitrary
signing or call target; no CLI path that raises a permission or an exposure, creates a new
LP position or increases liquidity, bypasses the risk gateway or pre-execution, or writes
into a version store the Web write path does not govern; no mainnet broadcast before T094;
no external asset transfer; no reuse of testnet economics as profitability evidence; no
automatic expansion after canary success.

## Tasks

- [T090 — Build the isolated encrypted-Keystore signer boundary](T090.md)
- [T091 — Build deterministic V4 transaction planning and preflight](T091.md)
- [T092 — Execute and reconcile the complete lifecycle on Robinhood testnet](T092.md)
- [T093 — Complete post-testnet paper/shadow validation and security review](T093.md)
- [T094 — Record explicit scoped mainnet promotion](T094.md)
- [T095 — Execute and reconcile a capped mainnet canary lifecycle](T095.md)
- [T096 — Produce final V1 acceptance and traceability dossier](T096.md)
- [T097 — Add the read-only query and reduce-only CLI commands](T097.md)
