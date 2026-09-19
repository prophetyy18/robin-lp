# V4 Position Sizing — Integer Sizing Discipline and Algorithm

> Task: T049. Layer: implementation spec (under `docs/implement/strategy/`,
> a Developer-authorable path outside the controller's `PROTECTED_PREFIXES`).
> The integer module, the 55-test pinned-vector suite, the pinned
> Solidity-derived vectors, and the re-export through
> `robinhood_lp.protocol` are the live artefacts this document specifies.

This document fixes the integer sizing discipline and algorithm that maps a
**fixed approved V4 Range** plus a per-side raw-token capital envelope to one
canonical integer `liquidityDelta` and the matching mint envelope, or to a
structured `NO_TRADE` verdict with a stable reason code. The sizer is a pure
function: no RPC, no storage, no clock, no randomness.

## 1. Scope and goals

The sizer is the single owner of `liquidityDelta` and the per-side mint cap.
It is invoked by the strategy layer with the approved Range, the current
`sqrtPriceX96`, and the per-side raw-token capital envelope, and it returns
exactly one of two outcomes:

- `OK` — a single integer `liquidityDelta` plus `amount0` / `amount1`,
  `amount0Max` / `amount1Max`, `minLiquidity`, `deadline`, and the
  boundary-inventory envelope. Both raw-token caps are respected, the
  Gas reservation is preserved, the Hook `BalanceDelta` (when verified) is
  included, and the simulated post-settlement wallet state never goes
  negative.
- `NO_TRADE` — a structured verdict carrying a stable
  `ReasonCode` (see [§10](#10-stable-reason-codes)). A `NO_TRADE`
  is **not** an exception.

The sizer implements G-V4-SIZING-01, G-LIMIT-01 and G-GAS-01 in their
integer form; ADR-004's integer/decimal two-domain policy governs the whole
path; ADR-014 governs the numeraire boundary that converts the USDG
reporting envelope to raw-token caps before sizing.

## 2. Numeraire boundary

The sizing mathematics is **numeraire-independent**: `liquidityDelta` and
both per-side `amount0` / `amount1` are derived from the fixed tick range,
the current `sqrtPriceX96` and canonical integer maths (ADR-004). No
reporting numeraire enters that computation. The integer discipline of this
task is therefore unchanged by the numeraire clause: one liquidity scale for
the whole position, no independent trimming of one side, no Range change,
the same rounding and the same worst-case single-sided-inventory verification
regardless of which numeraire the report is denominated in.

The reporting numeraire of ADR-014 applies only to the capital envelope and
to what a report displays. A cap stated in the dataset's reporting numeraire
is converted to a raw-token cap before sizing and must state its unit and
the time at which it is available; a `RELATIVE_ONLY` dataset states its
envelope in relative terms and carries no USD-denominated field. The active
execution pool's envelope remains USDG-denominated, and USDG remains the
execution numeraire. There is exactly **one named numeraire boundary** in
this module — `usdg_amount_to_token_amount` (§6), which uses Q64.64
fixed-point arithmetic exclusively and never touches `float` or `Decimal`.

## 3. Hard invariants

The sizer enforces the following invariants. Each invariant is a contract
clause and is reflected in a typed exception or a structured `NO_TRADE`
verdict; an invariant violation that does not match one of these outcomes
is a contract bug.

| # | Invariant | Enforcement |
| - | --------- | ----------- |
| I1 | The Range is fixed. The sizer never widens, narrows, shifts or re-aligns `(tickLower, tickUpper)` to make a cap pass. | `compute_sizing` rejects `tick_lower >= tick_upper` with `NO_TRADE` / `RANGE_DEGENERATE`; misalignment to the pool's `tickSpacing` raises `SizingAlignmentError`; `Range` is never an output of the cap-bounded step. |
| I2 | One liquidity scale. The whole position shares a single `liquidityDelta`; per-side trimming is forbidden. | `compute_sizing` returns one `liquidity` value; the post-settlement cap check uses that single value; there is no per-side `liquidityDelta0` / `liquidityDelta1`. |
| I3 | Integer-only. No `float` and no `Decimal` on the protocol/accounting path. | ADR-004; the integer module is audited in `test_protocol_module_has_no_float`. |
| I4 | Both raw-token caps are respected after Hook and Gas effects. | `compute_sizing` enforces `required_amount0 ≤ effective_amount0_cap` and `required_amount1 ≤ effective_amount1_cap`; the simulated post-settlement wallet state is non-negative. |
| I5 | Native ETH Gas reserve is preserved. | `_apply_gas_reservation` subtracts the reserve from the native-side cap once; the reserve is never spent on the LP itself. A reserve underflow yields `NO_TRADE` / `GAS_RESERVE_UNDERFLOW`. |
| I6 | Hook `BalanceDelta` is integer and verified. | A non-zero `hook_delta0` / `hook_delta1` without verified evidence raises `SizingHookError`; a verified charge is added to `required_amount*` before the cap check; a verified credit reduces it; an over-credit is rejected. |
| I7 | The Range is not moved to fit a cap. | Cap changes only ever reduce or hold `liquidity`; the structural test `test_lowering_caps_never_changes_ticks` asserts the cap step never mutates `(pa, pb)`. |
| I8 | Lowering either approved cap can only reduce `liquidity`, never change ticks. | Monotone non-decreasing in each cap; `compute_cap_bounded_liquidity` is closed-form, so no binary search is required and the monotonicity is exact. |
| I9 | Insufficient economic size → `NO_TRADE` / `INSUFFICIENT_ECONOMIC_SIZE`. | The USDG-equivalent gate compares the sized liquidity to the caller's minimum; below the minimum yields `NO_TRADE`. |
| I10 | Reason codes are stable strings. | `ReasonCode` is a `StrEnum`; renaming an existing value is a breaking change. |

## 4. Integer contracts

All inputs and outputs of the sizer are Python `int` (unbounded precision)
projected onto the V4 widths. The exact widths are enforced by typed
validators (`_require_uint160`, `_require_uint128`, `_require_int24`,
`_require_uint`) and are part of the integer contract:

| Field | Width | Sign | Domain |
| ----- | ----- | ---- | ------ |
| `sqrt_price_x96` | 160 bits | unsigned | `[MIN_SQRT_PRICE_X96, MAX_SQRT_PRICE_X96)` |
| `sqrt_price_a_x96`, `sqrt_price_b_x96` | 160 bits | unsigned | derived via `get_sqrt_price_at_tick` from `tickLower` / `tickUpper` |
| `liquidity` (output) | 128 bits | unsigned | `[1, MAX_UINT128]` |
| `liquidityDelta` | 128 bits | signed | modelled as `int` on the Python path; the on-chain Mint accepts `int128` |
| `amount0`, `amount1` | 256 bits | unsigned | floor (`round_up=False`) |
| `amount0Max`, `amount1Max` | 256 bits | unsigned | `≥ amount*`; the mint envelope sent on-chain |
| `tickLower`, `tickUpper` | 24 bits | signed | `[MIN_TICK, MAX_TICK]`; aligned to `tickSpacing` |
| `deadline` | 256 bits | unsigned | caller-supplied; not interpreted by the sizer |
| `minLiquidity` | 128 bits | unsigned | V1 default `0`; rejected if negative |
| `hook_delta0` / `hook_delta1` | 256 bits | signed | negative is a credit, positive is a charge |
| `gas_reserve_wei` | 256 bits | unsigned | subtracted from the native-side cap once |
| `accrued_fees0` / `accrued_fees1` | 256 bits | unsigned | added to the spendable cap on the post-settlement ledger |
| USDG amounts (caps, min-economic-size) | 256 bits | unsigned | atomic USDG units |

A field whose value falls outside its width raises `SizingInputError`. This
is a contract bug, never a market outcome.

## 5. Algorithm

`compute_sizing` runs the following steps. Every step is pure, integer-only
and uses the same `(tickLower, tickUpper)` it received — the Range is not
modified at any point.

1. **Validate inputs.** Every field is type-checked and width-checked;
   `tickLower` / `tickUpper` must lie in `[MIN_TICK, MAX_TICK]`; the
   alignment to `tickSpacing` is enforced (`SizingAlignmentError` on
   misalignment). `tickLower >= tickUpper` yields
   `NO_TRADE` / `RANGE_DEGENERATE`.
2. **Validate price.** `sqrt_price_x96` must lie in
   `[MIN_SQRT_PRICE_X96, MAX_SQRT_PRICE_X96)`. An out-of-domain price
   yields `NO_TRADE` / `PRICE_OUT_OF_BOUNDS`.
3. **Compute Range sqrt prices** via the integer tick-to-sqrt-price port
   (`get_sqrt_price_at_tick` from `robinhood_lp.protocol.math`).
4. **Classify the position** as `below` (`sqrt_price_x96 ≤ pa`), `inside`
   (`pa < sqrt_price_x96 < pb`) or `above` (`sqrt_price_x96 ≥ pb`).
5. **Hook verification.** A non-zero `(hook_delta0, hook_delta1)` without
   `hook_verified=True` raises `SizingHookError`. A verified delta is added
   to `required_amount*` in step 9.
6. **Native ETH Gas reservation.** When `currency0` is native
   (`Currency.native()`), `amount0_cap` is reduced by `gas_reserve_wei`
   once; the same applies to `currency1`. The reserve is never spent on
   the LP itself. A reserve underflow on either side yields
   `NO_TRADE` / `GAS_RESERVE_UNDERFLOW`.
7. **Both caps zero after Gas reservation → `NO_TRADE`.** When the wallet
   cannot fund either side after the Gas reservation, the verdict is
   `CAP_NONPOSITIVE` (or `GAS_RESERVE_UNDERFLOW` if the native side
   specifically ran into the reservation).
8. **Compute cap-bounded liquidity** (§5.1). If the closed-form result
   underflows to zero on both sides, yield `NO_TRADE` / `CAP_UNDERFLOW`.
9. **Compute per-side amounts** (`get_amounts_for_liquidity`, §5.2) and
   add the verified Hook `BalanceDelta` to form
   `required_amount0` / `required_amount1`. A delta that would push
   either required amount below zero raises `SizingHookError`.
10. **Post-hook cap check.** `required_amount*` must not exceed the
    Gas-reservation-adjusted caps; otherwise `NO_TRADE` / `CAP_UNDERFLOW`.
11. **Simulated post-settlement wallet state.** The post-settlement
    ledger is `effective_cap* + accrued_fees* − required_amount*`; both
    must be non-negative.
12. **Worst-case single-sided boundary inventory** at `pa` and `pb`
    (`worst_case_single_sided_inventory`, §5.3).
13. **Insufficient economic size gate.** When the caller supplies a
    `min_economic_liquidity_usdg_q64_64` and at least one non-zero
    `usdg_amount_token*`, the USDG-equivalent of the sized liquidity is
    compared to the minimum; below the minimum yields
    `NO_TRADE` / `INSUFFICIENT_ECONOMIC_SIZE`.
14. **Return the `SizingDecision` record** with the integer outputs.

### 5.1 Cap-bounded liquidity (monotone, closed-form)

For an **inside** position (`pa < p < pb`), the actual token brackets the
position consumes at the current price are `[pa, p]` for `token0` and
`[p, pb]` for `token1`. The per-side liquidity that exactly consumes the
cap is:

```
L0 = amount0_cap * pa * p / ((p − pa) << 96)
L1 = amount1_cap * Q96     / (pb − p)
```

The cap-bounded liquidity is `min(L0, L1)`, and the binding side is whichever
is smaller. The cap-bounded step is **monotone non-decreasing in each
cap** and is **exact** (closed-form); no binary search is needed because the
per-side formulas invert the corresponding `get_amount*_delta` calls used in
step 9. The V4 reference `LiquidityAmounts.getLiquidityForAmount0 /
getLiquidityForAmount1` use asymmetric brackets inside the Range that can
exceed the supplied cap when re-fed through `get_amounts_for_liquidity`; the
contract's "final simulated settlement stays below both raw-token/USDG
caps" clause forbids that path, so the sizer uses the **actual** brackets
above.

For a **below** position (`p ≤ pa`), the whole position is `token0`:

```
L0 = amount0_cap * pa * pb / ((pb − pa) << 96)
L1 = 0
binding_side = amount0
```

For an **above** position (`p ≥ pb`), the whole position is `token1`:

```
L0 = 0
L1 = amount1_cap * Q96 / (pb − pa)
binding_side = amount1
```

The result is monotone in each cap: lowering either `amount0_cap` or
`amount1_cap` can only reduce or hold the returned `liquidity`, and never
mutates `(pa, pb)`. The structural property is asserted by
`test_lowering_caps_never_changes_ticks` and the monotone property by
`test_lowering_amount0_cap_only_reduces_liquidity` and
`test_lowering_amount1_cap_only_reduces_liquidity`.

### 5.2 Per-side amounts (below / inside / above)

`get_amounts_for_liquidity(sqrt_price_x96, pa, pb, liquidity)` returns the
integer `(amount0, amount1)` the position consumes at the current price.
The three cases mirror V4's `LiquidityAmounts.getAmountsForLiquidity`:

- **below** (`p ≤ pa`): `amount0 = get_amount0_delta(pa, pb, L)`,
  `amount1 = 0`. The whole position is `token0`.
- **above** (`p ≥ pb`): `amount0 = 0`,
  `amount1 = get_amount1_delta(pa, pb, L)`. The whole position is `token1`.
- **inside** (`pa < p < pb`):
  `amount0 = get_amount0_delta(pa, p, L)`,
  `amount1 = get_amount1_delta(p, pb, L)`. Both floor (`round_up=False`).

The function is integer-only and pure; the same inputs always produce the
same outputs in any process. Pinned Solidity-derived vectors for each of
the three cases (and for a tight boundary Range) live in
`tests/fixtures/strategy/position_sizing_vectors.json` under
`below_range_amounts`, `inside_range_amounts`, `above_range_amounts`, and
`tick_boundary_sizes`; the Python module reproduces each vector
byte-exactly and matches the underlying Foundry oracle pinned at
`e50237c43811bd9b526eff40f26772152a42daba` in `tools/oracle/lib/v4-core`.

The module supports both **target / pair currency orderings**. The V4
canonical ordering requires `currency0 < currency1` (numerically as
`uint160`); the PoolKey validation enforces this at the identifier level
(`PoolKey.currency0` < `PoolKey.currency1`). The sizer takes the canonical
ordering as input and computes `amount0` / `amount1` against it; the
caller is responsible for naming the user-facing "target token" and
"pair token". The integer path is invariant to which side the user calls
"target": the cap labelled `amount0_cap` always maps to `currency0`,
and the cap labelled `amount1_cap` always maps to `currency1`.

### 5.3 Worst-case single-sided boundary inventory

`worst_case_single_sided_inventory(pa, pb, liquidity)` returns
`(amount0_at_pb, amount1_at_pa)`. At the **upper** boundary
(`sqrt_price_x96 == pb`) the position holds only `token0` and the amount is
`get_amount0_delta(pa, pb, L)`. At the **lower** boundary
(`sqrt_price_x96 == pa`) the position holds only `token1` and the amount is
`get_amount1_delta(pa, pb, L)`. These are the worst-case single-sided
inventory values the central risk layer (T070) compares against the
per-side exposure cap. They are pure functions of the Range and the
liquidity and never touch a price oracle.

## 6. USDG → raw-token conversion (Q64.64 boundary)

`usdg_amount_to_token_amount(usdg_amount, price_q64_64)` is the **single
named numeraire boundary** in this module. The function converts a USDG
cap (atomic units) to a raw-token cap (atomic units) using Q64.64
fixed-point arithmetic exclusively; no `float` and no `Decimal` enter
the integer domain.

```
amount_token = (usdg_amount << 64) // price_q64_64
```

The price `price_q64_64` is supplied by T053 as `usdg_per_token << 64`. The
rounding direction is **down**, so the resulting cap is conservative (the
user's USDG envelope is never exceeded). The function raises
`SizingUsdgConversionError` when:

- `price_q64_64` is non-positive (the price is supplied as a Q64.64
  unsigned fixed-point value);
- the conversion would overflow `uint256`.

Pinned vectors for the conversion live in
`tests/fixtures/strategy/position_sizing_vectors.json` under
`usdg_conversion`. They cover integer-round, round-down, and a
`price = 3 << 64` sanity check.

The sizer itself never sees a USDG amount on the cap path — only the
caller does. `compute_sizing` accepts optional
`usdg_cap_q64_64_token*` and `min_economic_liquidity_usdg_q64_64`
arguments, all of which are Q64.64 fixed-point integers; the integer
discipline is unchanged regardless of which numeraire is in use.

## 7. Mint envelope: `amount0Max`, `amount1Max`, `deadline`, `minLiquidity`

The sizer returns a `SizingDecision` whose `amount0Max` and `amount1Max`
are the per-side caps after the Gas reservation. The on-chain Mint
receives the same `amount*Max`; the user-side transaction may never
settle more than `amount*` per side, and the V4 router's slippage check
rejects a settlement that exceeds `amount*Max`. The sizer therefore
exposes both the integer `amount*` it expects to consume and the integer
`amount*Max` it permits the transaction to consume:

- `amount0Max = effective_amount0_cap = amount0_cap − gas_reserve_wei`
  when `currency0` is native, otherwise `amount0_cap`.
- `amount1Max = effective_amount1_cap = amount1_cap − gas_reserve_wei`
  when `currency1` is native, otherwise `amount1_cap`.
- `deadline` is the caller-supplied uint256 timestamp; the sizer does
  not interpret it but echoes it on the `SizingDecision`.
- `minLiquidity` is the caller-supplied uint128 minimum-liquidity
  protection (V1 default `0`). The integer module enforces the width
  but does not itself compare the sized `liquidity` against the
  minimum — the on-chain Mint's `minLiquidity` argument is the
  authoritative guard against "sized-but-uneconomic" mints. The
  insufficient-economic-size gate at step 13 is the user-side mirror.

## 8. Wallet leftovers and accrued fees

The simulated post-settlement wallet state is
`post_amount* = effective_cap* + accrued_fees* − required_amount*`.
Both `accrued_fees0` and `accrued_fees1` are added on the spendable side
of the ledger: the user is credited the accrued fees before the mint,
so the post-settlement state reflects what the wallet will actually hold
once the mint settles and the fees are claimed.

The Gas reservation is preserved by construction: the integer cap that
funded the position already had the reservation subtracted, so the
remaining wallet balance automatically includes the reservation. The
sizer does not call `payable{value: ...}` and never spends Gas on the LP
itself (G-GAS-01; must-not spend Gas reserve).

## 9. Reference to the integer implementation

The full implementation lives in
`src/robinhood_lp/protocol/sizing.py`; the public surface is re-exported
through `robinhood_lp.protocol`:

- `SizingDecision`, `ReasonCode`, `PositionRelative`, `BindingSide`
- `compute_sizing` (the canonical entry point)
- `get_amounts_for_liquidity` (below / inside / above Range amounts)
- `compute_cap_bounded_liquidity` (monotone closed-form cap-bounded
  liquidity, §5.1)
- `worst_case_single_sided_inventory` (worst-case single-sided boundary
  inventory, §5.3)
- `usdg_amount_to_token_amount` (Q64.64 USDG → token boundary, §6)
- `MAX_UINT128`, `MAX_UINT256` (V4 width sentinels)
- Typed errors: `SizingError` (base), `SizingInputError`,
  `SizingAlignmentError`, `SizingRangeError`, `SizingCapacityError`,
  `SizingHookError`, `SizingGasError`, `SizingUsdgConversionError`

The module depends only on `robinhood_lp.protocol.math` (the integer
math port from T012) and `robinhood_lp.protocol.ids` (the canonical
`Address` / `Currency` / `PoolKey` / `PoolId` types). It never imports
RPC, storage, configuration, or execution.

## 10. Stable reason codes

`ReasonCode` is a `StrEnum`. The values are part of the public contract;
new codes are additive; renaming an existing value is a breaking change.

| Code | Trigger |
| ---- | ------- |
| `RANGE_DEGENERATE` | `tick_lower >= tick_upper`. |
| `PRICE_OUT_OF_BOUNDS` | `sqrt_price_x96` outside `[MIN_SQRT_PRICE_X96, MAX_SQRT_PRICE_X96)`. |
| `CAP_NONPOSITIVE` | Both effective caps are zero and the cause is not a Gas reservation. |
| `CAP_UNDERFLOW` | Caps positive but the closed-form liquidity underflows to zero, or `required_amount*` exceeds `effective_amount_cap*` after the Hook check. |
| `HOOK_DELTA_NONZERO_UNVERIFIED` | (Internal / typed exception only — `SizingHookError`; not produced as `NO_TRADE`.) |
| `GAS_RESERVE_UNDERFLOW` | The native-side cap is strictly below the Gas reservation. |
| `INSUFFICIENT_ECONOMIC_SIZE` | The USDG-equivalent of the sized liquidity is below the caller's minimum. |
| `USDG_CONVERSION_FAILED` | (Internal / typed exception only — `SizingUsdgConversionError`.) |

## 11. Exception cases

The module distinguishes **contract bugs** (typed exceptions) from
**market outcomes** (structured `NO_TRADE` verdicts).

A typed exception is raised when:

- a field has the wrong Python type or width
  (`SizingInputError`);
- `tick_lower` / `tick_upper` is not aligned to the pool's `tickSpacing`
  (`SizingAlignmentError`);
- `sqrt_price_a_x96 >= sqrt_price_b_x96` after `get_sqrt_price_at_tick`
  (`SizingRangeError`);
- the closed-form liquidity would exceed `uint128`
  (`SizingInputError`);
- the Hook `BalanceDelta` is non-zero without verified evidence, or
  drives `required_amount*` negative (`SizingHookError`);
- the USDG conversion is requested with a non-positive price or would
  overflow `uint256` (`SizingUsdgConversionError`).

A structured `NO_TRADE` verdict is returned when:

- the Range is degenerate;
- the price is outside the V4 domain;
- both effective caps are zero;
- the cap-bounded liquidity underflows to zero;
- the post-hook required amounts exceed the caps;
- the native-side cap is below the Gas reservation;
- the sized liquidity is below the USDG-economic-size minimum.

## 12. Acceptance evidence

The acceptance clauses of the T049 contract are covered by the existing
artefacts:

- **Pinned Solidity vectors** for below / inside / above Range amounts,
  per-side liquidity, worst-case boundary inventory, and the Q64.64
  USDG → token conversion live in
  `tests/fixtures/strategy/position_sizing_vectors.json`. The vectors
  were produced by re-running the T012 Python port of V4 integer math
  against inputs and are matched byte-exactly by the sizer.
- **Lowering either approved cap can only reduce liquidity, never change
  ticks.** Asserted by
  `test_lowering_amount0_cap_only_reduces_liquidity`,
  `test_lowering_amount1_cap_only_reduces_liquidity`, and
  `test_lowering_caps_never_changes_ticks`.
- **Final simulated settlement stays below both raw-token/USDG caps
  after Hook and Gas effects.** Asserted by
  `test_simulated_settlement_respects_both_caps`,
  `test_native_gas_reservation_subtracts_once_from_cap`, and the
  post-hook `test_compute_sizing_verified_hook_charge_funded` /
  `test_compute_sizing_verified_hook_charge_exceeds_cap_returns_no_trade`
  / `test_compute_sizing_verified_hook_credit_reduces_required`.
- **Insufficient economic size returns `NO_TRADE`.** Asserted by
  `test_insufficient_economic_size_returns_no_trade` and
  `test_minimum_economic_size_passes_when_satisfied`.
- **Hook verification gate.** Asserted by
  `test_compute_sizing_unverified_hook_raises`,
  `test_compute_sizing_hook_credit_negative_required_raises`.
- **Gas reservation gate.** Asserted by
  `test_native_gas_reservation_zero_when_not_native`,
  `test_native_gas_reserve_underflow_returns_no_trade`,
  `test_native_gas_reservation_exact_match`.
- **Float-free invariant.** Asserted by
  `test_protocol_module_has_no_float`.
- **Determinism.** Asserted by `test_determinism` (same inputs →
  byte-identical `SizingDecision`, equal hashes).

The 55 pinned-vector and unit tests in `tests/test_position_sizing_t049.py`
together cover below / inside / above Range amounts (including tight-range
boundaries), worst-case single-sided boundary inventory, cap-bounded
liquidity, monotone sizing, USDG → token Q64.64 conversion, the Hook
`BalanceDelta` and Gas reservation gates, the insufficient-economic-size
gate, both-caps-zero, Range-degenerate, price-out-of-bounds, alignment,
determinism, float-freedom, reason-code stability, and
`SizingDecision` invariants.

## 13. References

- **G-V4-SIZING-01** — V4 positions are scaled by a single
  `liquidityDelta` over a fixed Range using canonical integer maths,
  worst-case single-sided inventory, Hook `BalanceDelta` and native
  ETH Gas reserve verification; the system does not silently change the
  Range to fit a cap. (Intent: `docs/intent/PROJECT_GOALS.md`.)
- **G-LIMIT-01** — Capital uses a global wallet cap and a
  strategy-authorised cap; the effective limit is the stricter one; a
  breach only blocks addition and reinvestment, not the open position.
- **G-GAS-01** — Gas reserve and Gas consumption are recorded and
  approved separately in USDG-equivalent terms; raw ETH amounts and
  on-chain fee evidence are retained.
- **ADR-004** — Integer / decimal precision policy. Two domains:
  integer (mandatory on the protocol / accounting path) and decimal
  (boundary only at a named display / statistical boundary).
- **ADR-014** — Research universe and numeraire. The reporting numeraire
  applies to the capital envelope and to display only; the sizing
  mathematics is numeraire-independent.
- **R3** — Stable identifiers and reason codes (referenced by ADR-004
  and the `ReasonCode` contract).
- **R8** — Capital and exposure control (referenced by G-LIMIT-01 and
  the per-side cap discipline).
- **R15** — Integer-only protocol accounting (referenced by ADR-004 and
  the float-free invariant).
