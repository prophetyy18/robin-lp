# T113 Backtest Application API — Implementation Guide

> **Audience.** A maintainer or operator of the V1 Robinhood LP
> framework who must consume or extend the T113 stable public
> backtest use case.
>
> **Authority.** This guide restates rules and contracts that
> live in the cited source documents
> (`docs/spec/architecture/ARCHITECTURE.md` §2.2 row T113,
> `docs/spec/architecture/COMPONENT_CONTRACTS.md` §§1–3, ADR-006
> and ADR-016, and task contract `T113.md`). Where this guide and
> a cited source disagree, the cited source wins. This guide
> does not introduce a new rule; it walks through the supported
> public boundary and the two heterogeneous pool fixtures the
> acceptance clause names.

This guide covers seven blocks. Each block ends with a citation
to the source document the rules are taken from.

1. [Public boundary](#1-public-boundary) — the supported import
   path and the typed operations / records.
2. [Port contracts](#2-port-contracts) — the typed adapter
   ports the use case composes against.
3. [Composition root](#3-composition-root) — how to wire the
   use case from a deployment or a test.
4. [Two heterogeneous pool fixtures](#4-two-heterogeneous-pool-fixtures)
   — Pool A (chain 4663) and Pool B (chain 8453).
5. [Failure semantics](#5-failure-semantics) — the named
   reason codes and the fail-closed behaviour.
6. [CLI integration](#6-cli-integration) — how the
   ``backtest start`` and ``backtest resume`` subcommands reach
   the boundary.
7. [Self-check](#7-self-check) — the contract-conformance suite
   and how to extend it.

---

## 1. Public boundary

The T113 boundary is the public import path
`robinhood_lp.application.backtest` declared in
`ARCHITECTURE.md` §2.2 row T113 (see
[`docs/spec/architecture/ARCHITECTURE.md`](../../spec/architecture/ARCHITECTURE.md)).
The boundary exports:

- `BacktestUseCase` — the typed class with `start` and `resume`
  operations (see
  [`src/robinhood_lp/application/backtest.py`](../../../src/robinhood_lp/application/backtest.py)).
- `BacktestRequest` — a stable alias for the
  `robinhood_lp.orchestrator.RunRequest` value object.
- `BacktestResult` — the terminal result the use case returns
  on every submission; never carries adapter internals.
- The typed failure classes (`BacktestUseCaseError`,
  `MissingDatasetRegistryFailure`,
  `PartitionResolutionFailure`,
  `UnqualifiedPartitionFailure`,
  `EmptyEventSourceFailure`,
  `InvalidRunRequestFailure`).
- `build_default_application` — the composition-root factory the
  CLI and the future Web page consumers depend on.

The boundary is the only supported composition path for new
product runs. The predecessor
`robinhood_lp.orchestrator.BacktestOrchestrator.submit` (the
T109 writer) is not reachable from any current CLI subcommand
(see §6).

> **Citations.** `docs/spec/architecture/COMPONENT_CONTRACTS.md`
> §§1–3 (public boundary, required contract contents, provider /
> consumer / composition rules); ADR-016 (contract-first
> component interfaces).

## 2. Port contracts

The use case accepts five typed adapter ports. None of the ports
are `Any` and none are arbitrary callables — every port is a
typed interface whose methods declare typed request and result
shapes (see `COMPONENT_CONTRACTS.md` §3).

| Port | Public type | Source | Required method |
| --- | --- | --- | --- |
| `store` | `RunStateStore` | `robinhood_lp.orchestrator` | `write`, `read`, `list_runs` |
| `dataset_resolver` | `DatasetResolver` | `robinhood_lp.orchestrator` | `resolve(dataset_version, chain_id, pool_key_id)` |
| `partition_resolver` | `T112DatasetPartitionResolver` | `robinhood_lp.orchestrator.t112` | `resolve(request, coverage)` |
| `event_source` | `T100ReplayEventSource` | `robinhood_lp.orchestrator.t112` | `load_events(chain_id, pool_key_id, block_range_start, block_range_end, cancel_token)` |
| `partition_ref_resolver` | `T100PartitionResolver` | `robinhood_lp.reports.t112` | `resolve(chain_id, pool_key_id, partition_ref)` |

Every port is at a dependency-safe location per ADR-006: the
orchestrator ports are at the `application / orchestration`
tier, the reports port is at the `backtest` tier, and the use
case (also at `application / orchestration`) imports them
upward-downward (downward from the higher tier, which is
allowed).

> **Citations.** ADR-006 (dependency direction); ADR-016
> (contract-first composition); `COMPONENT_CONTRACTS.md` §3
> (port placement).

## 3. Composition root

The composition root is the only place where concrete adapters
are wired behind the typed ports. Two roots exist:

1. **`build_default_application`** — the production / CLI
   composition. It accepts the five typed ports and instantiates
   the durable `RunStateStore` under `--runs-root`.
2. **`BacktestUseCase.__init__`** — the lower-level entry used
   by tests that pre-build a store with fixtures.

A deployment that supplies real T100 partitions wires them
through the T112 `partition_resolver` and `partition_ref_resolver`
adapters. A CLI invocation with no real adapters falls through
the fail-closed stubs the operator surface ships (see §5).

> **Citations.** ADR-016 (composition-root discipline);
> `COMPONENT_CONTRACTS.md` §3 (composition rules).

## 4. Two heterogeneous pool fixtures

The T113 acceptance clause names two heterogeneous pool
fixtures. The conformance suite uses them so the cutover is
pinned to genuinely distinct pools, not a single test case.

### 4.1 Pool A — chain 4663

```python
CHAIN_ID_A: int = 4663
POOL_KEY_A: str = "0x" + "ab" * 32
DATASET_HASH_A: str = "0x" + "ee" * 32
PARTITION_ID_A: str = (
    "chain=4663/contract=0xababababababababababababababababababab/"
    "event=Swap/range=1-100"
)
```

Pool A is the canonical V1 Robinhood Chain test pool. The
conformance suite binds the use case to Pool A through the
typed ports and asserts that the published T112 manifest
records `chain_id=4663` and `pool_key_id=POOL_KEY_A`.

### 4.2 Pool B — chain 8453

```python
CHAIN_ID_B: int = 8453
POOL_KEY_B: str = "0x" + "cd" * 32
DATASET_HASH_B: str = "0x" + "ff" * 32
PARTITION_ID_B: str = (
    "chain=8453/contract=0xcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd/"
    "event=Swap/range=200-300"
)
```

Pool B is the heterogeneous alternative: a different chain, a
different content hash, and a different partition reference.
The conformance suite binds the use case to Pool B through the
typed ports and asserts that the published T112 manifest
records `chain_id=8453` and `pool_key_id=POOL_KEY_B`.

> **Citations.** T113 acceptance clause (two heterogeneous pool
> fixtures).

## 5. Failure semantics

Every failure — missing registry, unresolved or unqualified
partition, empty input, replay / feature failure, cancellation —
fails closed with a named `T113_`-prefixed reason code. The
public surface never reaches a successful current artifact
under any failure path.

| Reason code | When |
| --- | --- |
| `T113_MISSING_DATASET_REGISTRY` | The dataset registry has no entry for the requested `dataset_version`. |
| `T113_PARTITION_RESOLUTION_FAILED` | The T100 partition resolver could not resolve the registered partition set. |
| `T113_UNQUALIFIED_PARTITION` | A partition reference was fabricated, unresolved, or failed its binding check. |
| `T113_EMPTY_EVENT_SOURCE_REFUSED` | The event source returned the empty event list. |
| `T113_INVALID_REQUEST` | The supplied request is invalid (closed vocabulary / required field). |
| `T113_UNEXPECTED` | An unexpected exception surfaced during composition. |

The T112 / T069 underlying reason codes are preserved on the
`error_message` field so a forensic reviewer can still trace
the original failure.

> **Citations.** T113 acceptance clause (fail-closed).
> `docs/spec/architecture/COMPONENT_CONTRACTS.md` §2 (closed
> vocabulary of named failures).

## 6. CLI integration

The CLI `backtest start` and `backtest resume` subcommands route
through the application use case. The CLI does not reach the
predecessor `BacktestOrchestrator` for those two operations;
inspection of `src/robinhood_lp/__main__.py` proves the cutover:
`_run_backtest` invokes `build_default_application` for the
`start` and `resume` paths and never constructs a
`BacktestOrchestrator`.

The CLI ships fail-closed stubs at the T112-specific ports so
an operator-driven `start` without a real T100 partition
registry is recorded as `FAILED` with the named
`T113_PARTITION_RESOLUTION_FAILED` reason. Production
operators inject real adapters through the application
composition root, not through CLI flags.

> **Citations.** T113 acceptance clause (CLI composition +
> old-path cutover).

## 7. Self-check

The contract-conformance suite lives at
[`tests/test_t113.py`](../../../tests/test_t113.py). The suite
pinned by this guide:

1. The public boundary exposes typed records and refuses
   untyped inputs (`TestPublicBoundary`).
2. Pool A and Pool B succeed through the boundary and publish
   T112-versioned artifacts
   (`TestNormalCaseTwoHeterogeneousPools`).
3. Boundary inputs fail closed
   (`TestBoundaryCases` — empty source, cancellation).
4. Invalid inputs raise the typed failure
   (`TestInvalidInput`).
5. Named failure modes fail closed
   (`TestFailureCases` — missing registry, unresolved
   partition).
6. The `resume` operation re-submits every `RUNNING` record
   (`TestResume`).
7. The CLI `start` and `resume` subcommands invoke the
   application use case; no current CLI path reaches the T109
   writer (`TestCLIRoutesThroughApplicationAPI`).
8. Historical T069 / T105 / T109 artifacts remain
   byte-identical (`TestByteIdenticalHistoricalArtifacts`).
9. The implementation guide exists and mentions the public
   boundary and the two pool fixtures
   (`TestImplementationGuide`).
10. The application boundary does not import lower-tier
    presentation / risk modules (`TestNoSiblingImports`).

The suite is the contract surface a reviewer inspects; any
change to the public boundary must update both the suite and
the guide.

> **Citations.** T113 acceptance clause (provider-conformance
> and CLI-consumer coverage, two heterogeneous pool fixtures,
> byte-identical historical artifacts).