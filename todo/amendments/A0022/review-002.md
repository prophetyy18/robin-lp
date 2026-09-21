# A0022 owner amendment review

- Base commit: `801050635f36ab141469750b103eef76902520bf`
- Candidate commit: `5174c7a84fc95612a4430206945b5a571b478a77`
- Verdict: **FAIL**

## Summary

Attempt 2 correctly closes all three defects recorded by review-001 at the planning-text level. It defines post-event MarketState and post-transition RunState, exact MarketCursor plus run_transition_ordinal bindings, pre-first/end-of-block and delayed-fill cases; makes T104 an explicit dependency and the sole fee-growth/range-fee projection with provenance; updates THREAT_MODEL and TRACEABILITY; and gives T109 explicit T101/T106/T102 compatibility-adapter and regression obligations. The full amendment remains properly scoped: relative to the original A0022 base it adds only T109, changes only the two revisions plus allowed collateral, and leaves every pre-existing task/config record and contract byte-identical. Gates add no finding: workflow validate, import graph, acceptance and diff-check pass, while the one citation/test failure is the same pre-existing T069 missing-module finding. Independent implementation inspection nevertheless finds a remaining contract contradiction. Current T061 handles a delayed fill synchronously while processing its earlier trigger: it searches a future fill-data event, immediately mutates the ledger, appends the future-timestamp fill, and only then continues the outer loop to any intervening market event. Therefore an intervening strategy callback can observe the post-fill ledger even though attempt 2 requires that fill to be absent from RunState until its later MarketCursor. Re-sorting saved transitions by MarketCursor can produce a cleaner logical timeline, but it cannot faithfully reproduce the ledger/information state the exact original simulation used. That conflicts with both exact-run replay and the acceptance claim that projected state equals state captured during the original T061 run. The Owner semantics already determine the answer: new runs must preserve exact historical-time causality through the one existing engine; no additional Owner decision or second engine is needed.

## Required changes

- Make delayed execution causality part of T109's successor obligation instead of only re-keying evidence after the run. Require the existing T061 engine path (not a second engine) to queue/schedule latency and fill transitions at their actual fill-data MarketCursor so no intervening market event, strategy callback, risk decision or accounting observation can see the fill before that cursor. Require the persisted audit ordinal and cursor order to agree for every state-changing transition, or fail publication; do not repair a non-monotonic original state history by sorting evidence after the fact. Add an acceptance case with trigger cursor A, an intervening reactive market cursor B, and fill cursor C: the strategy/accounting state actually supplied at B and RunState(B) must both be pre-fill, RunState(C) must be post-fill, the original-run captured state and replay projection must match at A/B/C, and the existing decision-risk-latency-fill, information-frontier and deterministic-rerun guarantees must remain passing. State explicitly that this is an extension/correction of the single T061 execution schedule used by new T109 runs; pre-T109 artifacts remain unavailable rather than being reinterpreted.

## Unknowns

- None.
