# Threat Model — V1

> Status: living document. Owned by Phase 0 / T004. Mutated only via a
> reviewable pull request and a paired CI test that fails if the
> underlying guarantee is broken.
>
> Scope: V1 paper-trading framework for a single Robinhood Chain
> Uniswap V4 `PoolKey` and a single user-selected target token. Live
> transaction submission is explicitly out of scope (see Phase 9, T090).

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
|                     V1 paper-trading process                    |
|                                                                  |
|  +-------------------+   +-----------------+   +-------------+  |
|  |  config + secrets |   |   RPC adapter    |   |   storage   |  |
|  |  (read-only)      |   |   (read-only)    |   |  (local)    |  |
|  +---------+---------+   +--------+--------+   +-------+-----+  |
|            |                      |                    |        |
|            v                      v                    v        |
|  +---------------------------------------------------------+    |
|  |  protocol / replay / features / strategy / risk / paper |    |
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
|  Robinhood Chain RPC  |               |   operator filesystem  |
|  (untrusted boundary) |               |   (untrusted)          |
+-----------------------+               +------------------------+
```

Boundary properties:

- The process holds **no signing key, no seed phrase, no API secret for
  broadcast or transaction submission**. This is enforced by CI
  (`tests/test_no_signing_paths.py`) and by ADR-007's pinned, minimal
  dependency set.
- All chain reads cross the RPC boundary as plain JSON-RPC requests
  that carry no credentials beyond a read-only API key (which is
  intentionally *not* a write token).
- The web console, when added (Phase 7+), is **not** in this trust
  diagram yet because V1 does not yet ship one. When it ships it must
  appear above the process box with explicit "no private key" rules.
- Local storage is *untrusted* from the process's point of view: data
  may be tampered with by an attacker with filesystem access. Mitigations
  are append-only layout, SHA-256 manifests, and re-derivation from raw
  partitions (T031).

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
| Internet-scale DoS against the process | Sustained traffic to a public endpoint | Out of V1 scope — no public surface yet |

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
  running two independent providers and halting on disagreement.
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
  contract address, never symbol; `docs/product/ASSET_ADMISSION.md`
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
  the ledger; T073 observability labels runs with mode.
- **Owner:** T071 (paper execution) + T073 (observability)
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
  `docs/product/ASSET_ADMISSION.md`; `target_token.decimals` is the
  only metadata assumed; other behaviors require explicit conditional
  models; T051 valuation gates on observed behavior.
- **Owner:** T070 + product owner
- **Residual risk:** Novel token behavior not yet enumerated.
  Mitigated by fail-closed defaults and human approval.
- **Evidence:** `docs/product/ASSET_ADMISSION.md` §5.

### T-11 Clock skew or non-monotonic time inside the process

- **Severity:** Medium
- **Scenario:** The process uses wall clock time or an unseeded random
  source during replay/backtest; results become non-deterministic and
  non-reproducible.
- **Controls:** ADR-006 forbids `time`/`random` in protocol/replay/
  features/strategy/backtest layers; a deterministic clock is injected
  (T061); `TODO.md` §2 mandates seed-controlled randomness.
- **Owner:** T061 (backtest engine)
- **Residual risk:** Libraries that pull wall clock internally (e.g.
  some logging libraries). Mitigated by dependency review and CI
  import inspection.
- **Evidence:** Phase 4 / 6 task contracts; ADR-006.

### T-12 Live-mode switch is silently available

- **Severity:** Critical (prevention)
- **Scenario:** A future code change accidentally introduces a
  `sendRawTransaction` or a private-key loader; V1 is promoted to
  live without authorization.
- **Controls:**
  1. `RunMode.LIVE` is rejected by `RootConfig` defaults
     (`tests/test_config.py::test_root_config_rejects_live_default_run_mode`).
  2. `tests/test_no_signing_paths.py` enforces that no module imports
     a write-capable API and no source file mentions forbidden
     identifiers.
  3. ADR-005 and `TODO.md` Phase 9 require an explicit, separately
     authorized project for live mode.
- **Owner:** T004 (this document) + ongoing
- **Residual risk:** None in V1. Phase 9 is a separate plan.
- **Evidence:** `tests/test_no_signing_paths.py`; `RunMode` enum.

## 4. Severity rubric

| Severity | Definition | Examples |
| --- | --- | --- |
| Critical | Wrong paper output that the user cannot detect; data corruption that propagates; secret exposure; unauthorized live-mode availability | T-01, T-02, T-03, T-04, T-12 |
| High | Wrong operator action enabled by the framework; leak of an identifier that is non-public but not a key | T-05, T-06 |
| Medium | Operability or correctness degradation that the user can detect and recover from | T-07, T-08, T-09, T-10, T-11 |
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
| T-10 | Hard gates + human approval | `docs/product/ASSET_ADMISSION.md` |
| T-11 | No wall clock in protocol layers | ADR-006 + import test |
| T-12 | Live-mode refusal + import/path scan | `tests/test_no_signing_paths.py` |

## 6. Residual risks and owner follow-ups

- **Hook-upgrade detection** depends on a code-hash comparison T072
  will add. Until T072 ships, paper mode can run on stale hook evidence.
  *Owner: T072.*
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
