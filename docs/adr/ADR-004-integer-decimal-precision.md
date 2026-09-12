---
id: ADR-004
title: Integer / decimal precision policy
status: proposed
date: 2026-09-12
owner: T000
supersedes: []
references: [R3, R14, R15]
---

# ADR-004 — Integer / decimal precision policy

## Context

Uniswap V4 protocol values (`sqrtPriceX96`, liquidity, tick indices,
token amounts, fee growth) are fixed-width integers on-chain. Float
arithmetic introduces rounding direction and precision loss that
cannot be reconciled with Solidity semantics. The framework must
match pinned canonical implementations exactly, and must allow
display/analysis code to use convenient units without contaminating
protocol accounting.

## Decision

Adopt a **two-domain policy**:

1. **Integer domain (mandatory).** Protocol, replay, reconstruction,
   features, valuation, and accounting code paths hold on-chain values
   as Python `int` (unbounded precision) or as project-typed wrappers
   (`Uint256`, `Int256`, `Tick`, `SqrtPriceX96`, `Liquidity`,
   `TokenAmount`). `float` is forbidden in these paths. Solidity
   rounding direction is preserved explicitly per operation.
2. **Decimal domain (boundary only).** Conversion to `Decimal` happens
   at an explicitly named display/statistical boundary
   (e.g. `to_display_price(sqrt_price_x96, decimals0, decimals1)`).
   The boundary is the only place that knows about token decimals and
   display units. Backwards conversion into the integer domain is
   forbidden except for round-trip tests with documented tolerance.

Round-trip and property tests (T012, T013) compare against pinned
Solidity/SDK oracles using integer equality wherever the canonical
implementation supports it; tolerance is permitted only where the
canonical implementation itself rounds.

## Alternatives considered

- **`Decimal` everywhere.** Matches display semantics but is not how
  Solidity computes and forces explicit precision arguments at every
  call site.
- **`float` / `numpy` / `mpmath`.** Convenient but introduces
  rounding-direction ambiguity and cannot match Solidity at integer
  bounds.
- **Custom fixed-point class.** Possible but adds a maintained surface
  that the project must keep aligned with V4 spec changes.

## Consequences

Positive:

- protocol code can be unit-tested for exact integer equality against
  pinned vectors;
- conversion to `Decimal` happens exactly once per value, making
  rounding auditable;
- Solidity rounding behavior is preserved by construction.

Negative / risks:

- integer-domain code is verbose without typed wrappers; T010–T012
  must land those wrappers before any non-trivial math is written;
- naive use of `/` on `int` in Python is floor division; every
  arithmetic helper must document its rounding direction or be a
  pure port of the Solidity original.

## Migration trigger

Re-evaluate this ADR if any of the following occur:

- a maintained, dependency-free, typed wrapper for EVM integer widths
  becomes the de facto standard in the Python ecosystem;
- the project needs high-throughput numeric kernels where typed
  wrappers become a measurable bottleneck (only then consider a
  narrow, justified use of vectorized fixed-point).

## Owner

T000 (initial). Hand off to whoever owns the protocol math module
introduced by T012.
