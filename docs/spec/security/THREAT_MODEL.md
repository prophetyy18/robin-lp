# Threat Model — V1

> Status: living document. Owned by Phase 0 / T004. Mutated only via a
> reviewable pull request and a paired CI test that fails if the
> underlying guarantee is broken.
>
> Scope: V1 = research → replay → backtest → testnet execution →
> post-testnet paper/shadow → mainnet automated execution, for a single Robinhood
> Chain `PoolKey` paired with one user-selected target token. Live
> automated execution is **in** V1 scope and is the V1 acceptance
> condition (G-LIVE-01); it must be unlocked only via the promotion
> gates defined in `docs/intent/PROJECT_GOALS.md` and the ADM-TECH-*
> hard gates in `docs/spec/product/ASSET_ADMISSION.md`.

## 1. Trust boundaries and data flow

```
+--------------------------+        +---------------------------+
|  operator / web console  |        |  user (target token input,|
|  (humans)                |        |  pool selection,          |
+----------+---------------+        |  approval decisions)      |
           |                        +-------------+-------------+
           |                                      |
           v                                      v
+------------------------------------------------------------------+
|                V1 main application (no key material)            |
|                                                                  |
|  +-------------------+   +-----------------+   +-------------+  |
|  | config + service  |   |   RPC adapter    |   |   storage   |  |
|  | secret references |   |   (read-only)    |   |  (local)    |  |
|  +---------+---------+   +--------+--------+   +-------+-----+  |
|            |                      |                    |        |
|            v                      v                    v        |
|  +---------------------------------------------------------+    |
|  | protocol / replay / features / strategy / risk / paper / Web| |
|  +--------------------------+------------------------------+    |
|                             |                                   |
|                             v                                   |
|                   +-------------------+                         |
|                   |  paper ledger     | (append-only, local)    |
|                   +-------------------+                         |
+------------------------------------------------------------------+
           |                                       |
           v                                       v
+-----------------------+               +------------------------+
|  Robinhood Chain RPC  |               | encrypted Keystore +   |
|  (untrusted boundary) |               | isolated signer/executor|
+-----------------------+               +------------------------+
```

Boundary properties:

- The main V1 application (config, replay, backtest, paper, web console)
  holds **no signing key, seed phrase, Keystore password, or capability to decrypt the
  Keystore**. Signing material lives only in a separate, isolated signer process
  (G-SIGNER-01). The main application can submit only a narrow, authenticated,
  state-bound request; it cannot read or export the key. Before Phase 9 this boundary is
  enforced by
  `tests/test_no_signing_paths.py` against the `src/robinhood_lp/`
  package, and by ADR-007's pinned, minimal dependency set.
- All chain reads cross the RPC boundary as plain JSON-RPC requests
  that carry no credentials beyond a read-only API key (which is
  intentionally *not* a write token). Live execution crosses a
  *separate* boundary via the signer process; the signer holds the key
  material and is reachable only through a deliberately narrow
  interface (built in Phase 9, T090; not present in the current
  release).
- The Web console is an untrusted input boundary even when locally deployed. It never
  receives a private key or Keystore password; risk-changing requests require session,
  CSRF, reauthentication, version and audit checks (T073, T084–T086). The reduce-only
  CLI write surface T097 is a second write entry into the same versioned store: it shares
  the version and audit binding, refuses every increase, and cannot bypass the risk
  gateway, pre-execution or the signer boundary.
- Local storage is *untrusted* from the process's point of view: data
  may be tampered with by an attacker with filesystem access. Mitigations
  are append-only layout, SHA-256 manifests, and re-derivation from raw
  partitions (T031).
- `docs/intent/PROJECT_GOALS.md` and `docs/spec/product/ASSET_ADMISSION.md`
  are **binding** for product scope; this threat model must align with
  them. Where they appear to disagree, the product documents win and
  this document is updated.

## 2. Adversary classes

| Class | Capability | In V1 scope? |
| --- | --- | --- |
| Malicious or compromised RPC provider | Returns forged blocks/logs/state | Yes — primary chain-data adversary |
| Compromised Python dependency | Executes arbitrary code on import or call | Yes — supply-chain adversary |
| Local filesystem attacker | Reads/writes `data/` partitions | Yes — local adversary |
| Co-located cloud account compromise | Same as local + secrets | Yes |
| Robinhood Chain operator (validator / sequencer) | Reorgs, censors, finality changes | Yes — chain-operator adversary |
| Hook contract upgrade or proxy swap | Pool behavior changes silently | Yes — hook-evolution adversary |
| Social engineering of the operator | Tricks operator into approving wrong action | Yes — operator adversary |
| Network passive observer | Sees plaintext RPC traffic (no body encryption) | Limited — RPC URLs only, no auth headers in logs |
| Internet-scale DoS against the process | Sustained traffic to a public endpoint | Limited — V1 requires no public Internet exposure, but the authenticated Web boundary is in scope |

## 3. STRIDE-classified threats

Each threat records: **severity**, **scenario**, **controls in V1**, **owner**,
**residual risk**, **evidence**.

### T-01 RPC provider returns forged data

- **Severity:** Critical
- **Scenario:** Provider serves `eth_getLogs` results that do not match
  the chain; the framework ingests them and reasons about a pool that
  does not exist.
- **Controls:** T020 cross-provider disagreement check; T024 chain
  capability report pins a block hash; T034 quality reports flag
  inconsistent coverage; cross-provider comparison during ingestion.
- **Owner:** T020 (rpc adapter) + T034 (data quality)
- **Residual risk:** Provider-level MitM during ingestion. Reduced by
  cross-endpoint comparison and halting on disagreement. Distinct endpoint brands
  are not assumed to be operationally independent; T034 records known common
  upstream infrastructure, so correlated compromise remains explicit residual risk.
- **Evidence:** `tests/test_no_signing_paths.py` denies write paths;
  T020 acceptance covers retry exhaustion and partial response.

### T-02 Reorg invalidates finalized data

- **Severity:** Critical
- **Scenario:** A deep reorg replaces blocks the framework already
  treated as final; downstream features and ledger reflect a non-
  canonical history.
- **Controls:** T033 confirmations/finality policy + orphan marking;
  T042 replay-vs-chain comparison at pinned blocks.
- **Owner:** T033 (reorg handling) + T042 (validation)
- **Residual risk:** Unfinalized reorgs that survive the window. V1
  records them but does not auto-correct.
- **Evidence:** Phase 4 task contracts and acceptance.

### T-03 Unknown or upgraded hook changes pool economics

- **Severity:** Critical
- **Scenario:** Hook contract is upgraded or its behavior changes; the
  framework's plain-pool assumptions are wrong; paper decisions are
  based on stale or absent model.
- **Controls:** T023 classifies pools with return-delta flags as
  ingestion-only by default; T043 hook semantics evidence packs gate
  promotion; T072/T080 halt paper on code-hash change.
- **Owner:** T023 (eligibility) + T043 (hook evidence)
- **Residual risk:** A subtle hook behavior change that is not detected
  by code-hash comparison (e.g. external oracle dependency). Mitigated
  by the user's `G-HOOK-01` rule and the human approval workflow.
- **Evidence:** `tests/test_config.py::test_pool_key_*` exercises V4
  hook address validity rules; T023 acceptance covers unknown hooks.

### T-04 Supply-chain compromise of a Python dependency

- **Severity:** Critical
- **Scenario:** A transitive dependency (`pydantic`, `pytest`,
  transitive) ships a malicious update; importing it gives the
  attacker code execution in the process.
- **Controls:** ADR-007 GitHub Actions pinned by SHA; OSV-Scanner on
  push and weekly; dependency review on PRs; small dependency surface
  (no blockchain/storage deps in T001/T002).
- **Owner:** T003 (CI) + future dependency review
- **Residual risk:** Pre-disclosure malware in a pinned SHA. Mitigated
  by the SHA-pinning + weekly OSV scan + small surface.
- **Evidence:** `.github/workflows/supply-chain.yml` runs gitleaks,
  trivy, OSV; `environment.yml` + `pyproject.toml` enumerate every
  dependency.

### T-05 Operator types or approves the wrong target token address

- **Severity:** High
- **Scenario:** User enters a wrong address or is phished into
  approving a malicious token; the system treats that token as V1's
  target.
- **Controls:** `target_token.user_approved` defaults to `False`;
  paper/live require explicit approval; the framework binds to
  contract address, never symbol; `docs/spec/product/ASSET_ADMISSION.md`
  records the technical-eligibility and project-risk workflow.
- **Owner:** T070 (risk) + product owner
- **Residual risk:** A sophisticated typo-squat or homoglyph address.
  Mitigated by the user's own cross-check discipline and the future
  web console's confirmation step (G-TARGET-01, G-TARGET-02).
- **Evidence:** `tests/test_config.py::test_target_token_default_user_approved_is_false`.

### T-06 Secret leaks via logs, reports, or committed files

- **Severity:** High
- **Scenario:** A credential-shaped URL or env-var value appears in
  CI logs, a thrown error, or an `os.environ` dump.
- **Controls:** `src/robinhood_lp/config/secrets.py` redacts URLs and
  sensitive env-var values; the loader walks parsed TOML and rejects
  credential-shaped strings at parse time; gitleaks scans every push.
- **Owner:** T002 (config) + T003 (CI)
- **Residual risk:** Secrets logged before reaching the framework (e.g.
  by a library's debug log). Mitigated by minimal dependency surface
  and CI secret scan.
- **Evidence:** `tests/test_config.py::test_loader_rejects_credential_url_in_config`
  and the four `tests/test_config.py` redaction tests; gitleaks in
  supply-chain workflow.

### T-07 Paper mode reads/stores data that looks like live balances

- **Severity:** Medium
- **Scenario:** Paper ledger stores simulated balances in the same
  layout as a future live ledger; an operator mistakes one for the
  other.
- **Controls:** T071 paper execution distinguishes simulated events in
  the ledger; T080 observability labels runs with mode.
- **Owner:** T071 (paper execution) + T080 (observability)
- **Residual risk:** Until T071 ships, no ledger exists, so this is
  preventive. Acceptance will require explicit `paper`/`live` labels
  on every event.
- **Evidence:** Phase 7 task contracts.

### T-08 Process denial-of-service from malformed or oversized logs

- **Severity:** Medium
- **Scenario:** A single `eth_getLogs` response carries too many logs
  or an oversized data field; the framework OOMs or hangs.
- **Controls:** T020 enforces a max block range per request; the rpc
  adapter splits ranges adaptively; quality reports flag oversized
  responses.
- **Owner:** T020
- **Residual risk:** Provider-side infinite-streaming on a single
  range. Mitigated by client-side timeouts.
- **Evidence:** Phase 2 T020 acceptance covers partial/malformed
  responses.

### T-09 Tampering of local append-only storage

- **Severity:** Medium
- **Scenario:** An attacker with filesystem access rewrites a raw
  partition file; the framework ingests tampered data on next run.
- **Controls:** T031 SHA-256 file manifests; T032 checkpoints compare
  manifest checksums before resuming; T030 round-trip tests detect
  schema drift.
- **Owner:** T031 (raw storage) + T032 (ingestion)
- **Residual risk:** Attack with both data and manifest access. Outside
  the threat model until the deployment context is defined.
- **Evidence:** Phase 3 task contracts.

### T-10 Process handles a token it cannot safely represent

- **Severity:** Medium
- **Scenario:** A token has fee-on-transfer, rebasing, blacklist, or
  proxy upgrade; the framework treats it like a plain ERC-20.
- **Controls:** `ADM-TECH-*` hard gates in
  `docs/spec/product/ASSET_ADMISSION.md`; `target_token.decimals` is the
  only metadata assumed; other behaviors require explicit conditional
  models; T051 valuation gates on observed behavior.
- **Owner:** T070 + product owner
- **Residual risk:** Novel token behavior not yet enumerated.
  Mitigated by fail-closed defaults and human approval.
- **Evidence:** `docs/spec/product/ASSET_ADMISSION.md` §5.

### T-11 Clock skew or non-monotonic time inside the process

- **Severity:** Medium
- **Scenario:** The process uses wall clock time or an unseeded random
  source during replay/backtest; results become non-deterministic and
  non-reproducible.
- **Controls:** ADR-006 forbids `time`/`random` in protocol/replay/
  features/strategy/backtest layers; a deterministic clock is injected
  (T061); `todo/README.md` §2 mandates seed-controlled randomness.
- **Owner:** T061 (backtest engine)
- **Residual risk:** Libraries that pull wall clock internally (e.g.
  some logging libraries). Mitigated by dependency review and CI
  import inspection.
- **Evidence:** Phase 4 / 6 task contracts; ADR-006.

### T-12 Live-mode switch bypasses promotion gates

- **Severity:** Critical
- **Scenario:** A future code change accidentally enables live
  automated execution without satisfying the V1 promotion gates
  (backtest → testnet → post-testnet paper/shadow → security review → human promotion;
  G-LIVE-GATE-01), or leaks signing material into the main V1
  process in violation of G-SIGNER-01.
- **Controls:**
  1. Through Phase 8, `tests/test_no_signing_paths.py` enforces that no source file in
     `src/robinhood_lp/` imports a write-capable API or mentions
     signing-related identifiers. T090 replaces this temporary absence proof with a
     separately packaged signer and a narrow authenticated request boundary; T091/T092
     add a separately constrained planner/executor, not key access to the main app.
  2. Before T094, `RootConfig` rejects `default_run_mode = "live"` and any pool
     configured at `RunMode.LIVE` (`tests/test_config.py`). T094 replaces this temporary
     refusal with verification of an immutable scoped promotion record; live never
     becomes a default.
  3. Every `TargetTokenConfig` carries an explicit dual-track
     approval state (`technical_eligibility`, `project_risk`,
     `user_decision`) and a `live_eligible` boolean that the live
     execution path (T070+) requires to be True before any live
     intent is accepted.
  4. Hook or token code-hash changes immediately demote the pool
     (ADM-HOOK-005, ADM-TECH-007) and stop live intents; the demotion
     is auditable, not silent.
- **Owner:** T004 + T070 + T090–T095 + product owner
- **Residual risk:** Until the signer process and T070 risk gateway
  ship, the main process contains no signing path at all, so the
  threat is *absent* in the current release rather than mitigated.
  This is consistent with G-SIGNER-01's hard isolation rule.
- **Evidence:** `tests/test_no_signing_paths.py`; `tests/test_config.py`;
  `docs/spec/security/THREAT_MODEL.md` §1; ADM-HOOK-005, ADM-TECH-007, G-LIVE-GATE-01,
  G-SIGNER-01.

### T-13 Unauthorized or stale Web control request

- **Severity:** Critical
- **Scenario:** An attacker, stale browser tab, replayed request or CSRF changes a token,
  PoolKey, strategy, limit, promotion or pause decision without the owner's current intent.
- **Controls:** authenticated local deployment; CSRF protection; reauthentication for
  risk-changing operations; optimistic concurrency; immutable version/audit binding;
  deny on timeout or ambiguous result (T073, T084–T086); the reduce-only CLI write
  surface (T097) writes through the same versioned store and audit event and refuses any
  increase, so it is not a second authority.
- **Owner:** T073 + T084–T086 + T097
- **Residual risk:** Compromise of the operator's authenticated workstation/session, or of
  the local CLI session.
- **Evidence:** `WEB-GLOBAL-003`, `CTRL-CLI-001`, T073/T085/T086/T097 acceptance.

### T-14 Forged, replayed, or stale signer/executor request

- **Severity:** Critical
- **Scenario:** A valid signature is obtained for changed calldata, chain, PoolKey, value,
  nonce, fee, deadline or stale simulation, or a request is executed twice.
- **Controls:** T090 authenticated request schema binds every field and authorization;
  T091 deterministic decoding/preflight; T092/T095 idempotent nonce, replacement,
  finality and reconciliation evidence. The reduce-only CLI surface T097 issues its
  LP-side requests through the same schema, preflight and signer boundary, and a plan
  that fails pre-execution is not submitted.
- **Owner:** T090–T095 + T097
- **Residual risk:** A compromise spanning both authorization storage and isolated
  execution services; mitigated by limits, independent reconciliation and kill switches.
- **Evidence:** Phase 9 acceptance, G-EXEC-01 and T097 acceptance.

### T-15 Keystore, password, or backup exposure

- **Severity:** Critical
- **Scenario:** Plaintext key/password reaches environment, Web, logs, shell history,
  process arguments or backups, or a restored signer unlocks unattended.
- **Controls:** standard encrypted Keystore; hidden interactive password input; plaintext
  only in signer memory; address-only backup verification; restart locked; secret scans;
  compromise runbook (T081, T090).
- **Owner:** T081 + T090
- **Residual risk:** Host/root or memory compromise while signer is unlocked.
- **Evidence:** G-SIGNER-01, ADR-003 and T090 acceptance.

### T-16 Manipulated or unavailable USDG valuation

- **Severity:** Critical
- **Scenario:** An activity-pool spot price, stale quote, depeg assumption or bad indirect
  path understates exposure/loss and admits a dangerous LP or Swap.
- **Controls:** point-in-time provenance and availability semantics (T053); independent or
  quality-qualified conversion sources; USDG depeg handling; missing/stale valuation blocks
  new risk; raw token amounts remain authoritative (T049, T070).
- **Owner:** T049 + T053 + T070
- **Residual risk:** Correlated manipulation of all approved sources.
- **Evidence:** ADM-POOL-002, G-VALUATION-01 and T053/T070 acceptance.

### T-17 Label leakage or look-ahead in the research panel

- **Severity:** Critical
- **Scenario:** A panel feature, label or split carries information that was
  not available at its decision time — a label window overlapping the training fold
  without a purge gap, a statistic computed over the evaluation period, or a feature
  stamped by data time rather than availability time. The model then appears
  predictive and is not, and the false skill feeds range, sizing or promotion
  decisions.
- **Controls:** `DS-020`/`DS-021` admit only pool holdout, time holdout and
  walk-forward as split axes; `DS-022` requires a purge and embargo gap whose length
  is derived from the declared label horizon and recorded with every split; T050
  point-in-time bars and T101 labels carry their observation and availability times;
  T101 acceptance fails a run when an injected future-derived feature or an unpurged
  overlapping fold is detected; T063/T105 record predecessor dataset bindings, while
  T109 binds current manifests and simulation evidence to immutable dataset references,
  content hashes, exact canonical cursors and registered strategy revision; T106
  constrains robustness surfaces to the recorded registered schema and ranges.
- **Owner:** T101 (panel labels and harness) + T064 (historical split/robustness
  delivery) + T106 (current schema-bound split/robustness successor after retirement)
  + T050 (point-in-time bars) + T109 (current manifest/run-evidence successor after retirement)
- **Residual risk:** A leak that the artifact's own metadata does not reveal, such as
  a source whose availability time is itself misrecorded. Mitigated by re-derivation
  from raw partitions and manifest checksums; not eliminated.
- **Evidence:** T101 acceptance (injected future-derived feature, overlapping label
  window, truncation invariance); T109/T106 acceptance, approved-consumer regression and
  migration/old-path-unreachable evidence;
  `docs/spec/research/DATASET_AND_EVALUATION.md` §4 (`DS-020`–`DS-022`).

### T-18 A `RELATIVE_ONLY` research artifact is read or reported as USD-denominated

- **Severity:** High
- **Scenario:** A dataset whose currencies include neither a USD asset nor a
  qualified conversion route carries relative results only, but a result, chart or
  export presents it with a USD unit, a dollar sign or an implied USD PnL, and an
  operator reads relative performance as a dollar result.
- **Controls:** ADR-014 clause 3 and `DS-003` forbid any USD-denominated field
  anywhere in a `RELATIVE_ONLY` dataset; `WEB-GLOBAL-001` requires every page to show
  the dataset version and the reporting numeraire and makes `RELATIVE_ONLY` textually
  and visually distinguishable; T053 records the qualification per numeraire; T052
  keeps attribution relative; T084, T086 and T103 acceptance require the distinction
  on every view that displays a result.
- **Owner:** T053 (valuation qualification) + T084/T086 (console display) + T103
  (research pages and saved definitions)
- **Residual risk:** A hand-built export or a screenshot taken out of context loses
  the marker; the dataset's qualification record is the only durable evidence.
- **Evidence:** `WEB-GLOBAL-001`, ADR-014 clause 3, `DS-003`, T084/T086/T103
  acceptance.

### T-19 A research artifact or model acquires execution authority

- **Severity:** Critical
- **Scenario:** A dataset, model, saved research definition or model assessment is
  treated as an execution approval, appears in an approval list, becomes the active
  strategy default, or is reachable from an execution-shaped control, so an
  unreviewed model reaches paper or live.
- **Controls:** ADR-014 clauses 1 and 5 and `G-ML-01`; T085's versioned write path
  records a research definition as a research artifact, and its acceptance forbids
  one from being read as, converted into, or offered as an execution approval and
  forbids an execution-shaped control from creating, promoting or referencing one;
  T103 may not let a definition become an execution approval or appear in an
  execution-shaped control; T086 journeys prove that no research page is reachable
  from, or confusable with, an execution-shaped control; T070 and T094 admit an
  intent only against an explicit scoped approval; T102 may not let a model become
  the live default.
- **Owner:** T085 (write path) + T103 (research pages) + T086 (journeys) + T070/T094
  (the execution gate)
- **Residual risk:** An operator who copies a model assessment into a live parameter
  by hand; outside the framework's reach, but visible in the version/audit trail.
- **Evidence:** T085 acceptance; T086 journeys; `WEB-GLOBAL-003`; ADR-014 clauses 1
  and 5; `G-ML-01`.

### T-20 Model output bypasses or weakens the central risk gateway

- **Severity:** Critical
- **Scenario:** A model-backed regime or fee-opportunity implementation is consulted
  before the central risk check instead of after it, its uncertainty is converted
  into a relaxed limit, or a second, weaker check is placed ahead of the gateway, so
  an intent the gateway would refuse is created or its limits are widened.
- **Controls:** `G-RISK-01` requires one independent, central and non-bypassable
  check; T070 owns the gateway that admits intents; the T060 component contract
  confines a model to the regime and fee-opportunity interfaces and forbids it from
  altering risk semantics, while T102's must-not clauses forbid bypassing, weakening
  or duplicating the gateway and its acceptance re-verifies that substituting the
  model changes no ledger, risk, execution or audit contract; ADR-006 keeps strategy
  free of RPC, storage and execution imports.
- **Owner:** T070 (risk gateway) + T060 (strategy contract) + T102 (model components)
- **Residual risk:** A future task that adds a second admission path ahead of the
  gateway; the contract text forbids it, and only the T102 substitution test and T070
  acceptance would detect it.
- **Evidence:** T070 and T102 acceptance; `G-RISK-01`; ADR-006.

### T-21 A published dataset version, numeraire or split boundary is mutated

- **Severity:** Medium
- **Scenario:** A published dataset version has its member pools, block ranges,
  reporting numeraire, valuation qualification or split boundaries edited in place,
  so a published result can no longer be reproduced and its stated provenance is
  wrong.
- **Controls:** ADR-014 clause 4 and `DS-002` make publishing additive and forbid
  editing an existing version; `DS-043` requires every model artifact to carry a
  content hash and full provenance (dataset version, feature configuration, split
  definition, hyperparameters, seed, code revision); T063/T105 historical manifests
  record predecessor dataset versions and reporting numeraires, while T109 is the
  current registry-bound manifest and run-evidence authority after retirement, references
  immutable T100 partitions instead of copying the market timeline, and fails validation when
  the dataset/numeraire, partition/content hash, cursor or registry/schema binding is missing or
  disagrees; T100's registry and T103's
  saved definitions re-open to the same pools, ranges and segment roles; T031/T032
  compare partition manifests and checksums.
- **Owner:** T100 (dataset registry) + T063/T105 (historical manifest deliveries) + T109
  (current manifest, simulation-evidence and replay successor after retirement) + T101 (artifact provenance) + T103
  (saved definitions)
- **Residual risk:** A mutation is detected only when a run is repeated; without a
  durable record of the original version, tampering that also rewrites the manifest
  cannot be proven.
- **Evidence:** `DS-002`, `DS-043`, ADR-014 clause 4, T101/T103 acceptance, and
  T109 compatibility, approved T101/T106/T102 regression, migration and
  old-path-unreachable acceptance.

### T-22 A completed run is replayed with substituted market or strategy state

- **Severity:** Critical
- **Scenario:** A reader combines a run with a different dataset, pool or cursor, infers run
  transitions from integer timestamps, re-executes the current strategy, substitutes a final
  summary for intermediate state, exposes a future delayed fill to an intervening callback, sorts
  a non-causal audit history after the run, or uses an alternative fee calculation. The resulting frame
  looks historical but does not represent either canonical market truth or what that exact run did.
- **Controls:** `DS-005`–`DS-007` bind post-event MarketState and post-transition RunState to one
  immutable dataset/content hash, exact canonical cursor and deterministic transition ordinal;
  T109 publishes checksummed run evidence atomically, refuses unbound transitions, never invokes
  strategy callbacks for replay and composes rather than copies the two timelines; new T109 runs
  queue delayed latency/fill inside the single T061 schedule until the actual fill-data cursor, and
  require audit ordinal/cursor agreement at creation rather than post-sort repair; T104 remains the
  sole fee-growth/range-fee projection with its dataset/window/cursor/reconstruction provenance;
  pre-evidence runs are explicitly unavailable instead of regenerated.
- **Owner:** T100 (canonical dataset identity) + T104 (fee-growth projection) + T109 (current
  manifest, evidence, state projection and frame composition successor after retirement)
- **Residual risk:** A defect in the original engine/accounting transition is preserved faithfully
  by evidence. Replay proves what the run did, not that the run's economic logic was correct; T061,
  T052 and independent reconciliation remain responsible for that correctness.
- **Evidence:** G-HISTORICAL-REPLAY-01, G-RUN-REPLAY-01, `DS-005`–`DS-007`, T104 equivalence and
  provenance acceptance, and T109 cursor-order, no-callback, tamper, compatibility, migration and
  old-path-unreachable acceptance.

## 4. Severity rubric

| Severity | Definition | Examples |
| --- | --- | --- |
| Critical | Wrong economic output that the user cannot detect; data corruption; secret exposure; unauthorized control or live execution | T-01, T-02, T-03, T-04, T-12–T-17, T-19, T-20, T-22 |
| High | Wrong operator action enabled by the framework; leak of an identifier that is non-public but not a key | T-05, T-06, T-18 |
| Medium | Operability or correctness degradation that the user can detect and recover from | T-07, T-08, T-09, T-10, T-11, T-21 |
| Low | Cosmetic, performance, or recoverable nuisance | (none in V1) |

## 5. Controls matrix

| Threat | V1 control | Test / evidence |
| --- | --- | --- |
| T-01 | Cross-provider disagreement, manifest pins | Phase 2/3 acceptance |
| T-02 | Finality window, orphan journal | Phase 3/4 acceptance |
| T-03 | Ingestion-only default on return-delta hooks; T043 evidence pack | `test_pool_key_*` |
| T-04 | Pinned actions, OSV scan, small surface | `.github/workflows/supply-chain.yml` |
| T-05 | `user_approved=False` default; address binding | `test_target_token_default_user_approved_is_false` |
| T-06 | Secret redaction + parser rejection + gitleaks | `test_*redact*`, `test_loader_rejects_credential_url_in_config` |
| T-07 | Ledger mode labeling | Phase 7 acceptance |
| T-08 | Range splitting, timeouts | Phase 2 acceptance |
| T-09 | Manifest checksums | Phase 3 acceptance |
| T-10 | Hard gates + human approval | `docs/spec/product/ASSET_ADMISSION.md` |
| T-11 | No wall clock in protocol layers | ADR-006 + import test |
| T-12 | Live-mode refusal + import/path scan | `tests/test_no_signing_paths.py` |
| T-13 | Authenticated/versioned Web and reduce-only CLI writes | T073/T085/T086/T097 evidence |
| T-14 | Bound signer request + deterministic planner/executor | T090–T095, T097 evidence |
| T-15 | Encrypted Keystore + interactive unlock + locked restart | T081/T090 evidence |
| T-16 | Qualified point-in-time USDG quotes + fail-closed risk | T049/T053/T070 evidence |
| T-17 | Point-in-time features/labels, horizon-derived purge/embargo, temporal-only splits, current dataset-reference manifest binding | T101/T106/T109 acceptance; `DS-020`–`DS-022` |
| T-18 | Qualification record + `RELATIVE_ONLY` display rule (no USD-denominated field) | `WEB-GLOBAL-001`; T084/T086/T103 acceptance |
| T-19 | Research artifacts stay research: no execution authority, approval appearance or reachability | T085 acceptance; T086 journeys; ADR-014 clauses 1 and 5 |
| T-20 | One central, non-bypassable risk gateway ahead of any model output | T070 + T060/T102 acceptance; `G-RISK-01` |
| T-21 | Additive publishing, content hashes, canonical dataset references and dataset/registry-version binding in the manifest/evidence | T100/T063/T105 historical evidence plus T109/T101 acceptance; `DS-002`/`DS-043` |
| T-22 | Exact cursor/ordinal binding, causally queued delayed fills, immutable run evidence, no post-sort/strategy rerun, T104-only fee projection | T061/T104/T109 acceptance; `DS-005`–`DS-007` |

## 6. Residual risks and owner follow-ups

- **Hook-upgrade detection** must immediately demote the pool per
  ADM-HOOK-005. The runtime hook-evidence package (T043) records the
  code hash and version; any change recorded by T072 forces
  re-verification and stops new paper/live intents until the human
  approval workflow re-runs. Until T043 + T072 ship, hook evidence is
  a single on-chain read at promotion time and a stale-cache risk
  remains; the framework refuses promotion above `ingestion` for any
  hook pool without a current T043 evidence pack.
  *Owner: T043 (evidence) + T072 (runtime detection) + T070 (gate).*
- **Target-token dual-track approval.** `TargetTokenConfig` carries
  three independent fields (`technical_eligibility`,
  `project_risk`, `user_decision`). Until T070 wires these into the
  risk gateway, the config layer enforces them at parse time but does
  not run the supporting chain reads; the live execution path will
  refuse intents whose triple is not
  `ELIGIBLE/≤VERY_HIGH/APPROVED*` AND whose `live_eligible` flag is
  False. *Owner: T070.*
- **Signer isolation** depends on Phase 9 (T090). Until then, the threat is reduced by
  absence of signing/broadcast capability. T090–T092 must replace that temporary control
  with process isolation, request authentication and testnet evidence. *Owner: T090–T092.*
- **Multi-provider verification** is a T020 stretch; until it ships
  T-01 is mitigated only by the chain-capability report and quality
  reports. *Owner: T020.*
- **Backup/restore** has no policy until T081. *Owner: T081.*
- **Finality policy per chain** has a placeholder (`confirmations=12`).
  Robinhood Chain's actual finality behavior is verified by T024.
  *Owner: T024.*
- **No human owner yet.** All "owner" fields are placeholders; once a
  human release authority is named, every row should be revisited.

## 7. Update procedure

This document is updated only via a reviewable change that:

1. adds or removes a threat with severity and owner;
2. changes a control that is paired with a test (the test must change
   in the same commit);
3. introduces a new external fact (a chain, a protocol upgrade, a
   dependency) — the source URL, retrieval time, and reference ID must
   be recorded.

No threat is silently downgraded. Severity changes require an explicit
note in the change history.
