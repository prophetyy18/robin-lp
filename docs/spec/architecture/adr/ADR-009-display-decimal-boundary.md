# ADR-009 — Display / Decimal boundary

> Status: Accepted
> Date: 2026-09-15
> Deciders: Robinhood-LP Owner (Direction 1, answer A)

## Context

`docs/spec/architecture/ARCHITECTURE.md` §3 forbids `float` in protocol / replay /
features / valuation / accounting code paths. ADR-004 already mandates that
conversion to `Decimal` happen at an explicitly named display/statistical
boundary and that backwards conversion into the integer domain is forbidden
except for round-trip tests. The implementation, however, retained
`sqrt_price_x96_to_price(int) -> float` and `price_to_sqrt_price_x96(float) -> int`
inside `src/robinhood_lp/protocol/math.py` and re-exported them from
`src/robinhood_lp/protocol/__init__.py`. The Owner has directed (Direction 1,
answer A) that the protocol core's float API not be retained.

## Decision

The `float` API in `src/robinhood_lp/protocol/` is removed entirely. The
display-boundary module is `src/robinhood_lp/presentation/prices.py`
(stub at the time of this ADR; concrete module name to be confirmed when
T012 attempt 2 lands; ADR-009 is amended in place if the module name
changes). The single Decimal-typed display helper is
`to_display_price(sqrt_price_x96: int, decimals0: int, decimals1: int) -> Decimal`.
It is the only function in the V1 codebase permitted to convert between
the integer protocol domain and the `Decimal` display domain. The
helper's inverse (display price -> integer protocol domain) does not
exist in V1; users needing an inverse compute it from a USDG source via
T053 (which is post-P01).

`src/robinhood_lp/protocol/__init__.py` MUST NOT re-export `float`-typed
helpers. `src/robinhood_lp/protocol/` MUST NOT import `Decimal`. The
display-boundary module MAY import `Decimal`.

## Consequences

Positive:
- The protocol package's public API is float-free. Downstream code that
  needs a display price imports `Decimal` and the display helper
  explicitly.
- The boundary is named (one module, one function) and matches the
  ADR-004 "explicitly named display/statistical boundary" requirement.
- A future V2 that needs a different display boundary knows to amend
  ADR-009 in place rather than scatter float conversions across the
  codebase.

Negative:
- T012 attempt 2 must delete the two float helpers and add the new
  Decimal helper. Existing tests must be amended. The work is bounded
  and can be done in one Developer attempt.
- Any out-of-tree consumer that today imports
  `robinhood_lp.protocol.sqrt_price_x96_to_price` or
  `robinhood_lp.protocol.price_to_sqrt_price_x96` must update its
  import to `robinhood_lp.presentation.prices.to_display_price` (or
  stop importing — these functions are display-only and may not be
  needed at all).

Reversibility: this decision is reversible in the sense that ADR-009
can be amended in place to relocate the display helper, and a new
helper can be added at the same boundary. The decision is NOT reversible
in the sense of re-introducing `float` symbols into the protocol
package: any such change requires a new ADR with stronger evidence than
this one carries.