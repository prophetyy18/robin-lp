# T049 independent review

- Base commit: `12464ed390f2216d749d54a76aca4f24b14c681c`
- Candidate commit: `52923f21ca39bcd24b8b71d25d3add809b804a6a`
- Verdict: **PASS**

## Checks

### contract-text-relocated-deliverable — PASS

Contract first Deliverable was successfully relocated from the protected `docs/spec/strategy/V4_POSITION_SIZING.md` to the Developer-authorable `docs/implement/strategy/V4_POSITION_SIZING.md` per the planning amendment. Dependencies, Outcome, Acceptance, Must-not, and References are unchanged from base.

Evidence:

- git diff 12464ed..52923f2 -- todo/phases/P05-features-and-valuation/T049.md shows the Deliverables paragraph now lists `docs/implement/strategy/V4_POSITION_SIZING.md` (Developer-authorable) with explanatory text noting docs/spec/ is reserved for PROPHET
- todo/triage/P05/T049/triage-001.json classifies the prior attempt as CONTRACT_MISMATCH; todo/reviews/P05/T049/plan-review-001.json verdict PASS approves the relocation; todo/evidence/P05/T049/attempt-001-planner.json records the planning outcome
- No other contract sections (Dependencies, Numeraire boundary, Outcome, Acceptance, Must-not, References, Contract status banner) are modified

### spec-doc-present-and-complete — PASS

The spec doc covers numeraire boundary, hard invariants, integer contracts, below/inside/above algorithm, both target/pair orderings via the canonical ordering rule, monotone sizing, wallet leftovers/accrued fees, worst-case single-sided boundary inventory, Hook BalanceDelta, native ETH Gas reservation, amount0Max/amount1Max/deadline/minLiquidity, exception cases, and cross-references to G-/ADR-/R-* sources. All required contract sections are present.

Evidence:

- docs/implement/strategy/V4_POSITION_SIZING.md exists (456 lines) with 13 numbered sections: Scope and goals, Numeraire boundary, Hard invariants (10 invariants I1-I10), Integer contracts, Algorithm (with below/inside/above), USDG → raw-token conversion (Q64.64 boundary), Mint envelope, Wallet leftovers and accrued fees, Reference to integer implementation, Stable reason codes, Exception cases, Acceptance evidence, References
- Section 2 covers the numeraire boundary with the exact language from the contract (numeraire-independent sizing, USDG reporting → raw-token conversion, RELATIVE_ONLY carries no USD field, USDG remains the execution numeraire)
- Section 3 lists all 10 hard invariants (Range fixed, one liquidity scale, integer-only, both caps respected, Gas reserve preserved, Hook delta verified, Range not moved, monotone cap, insufficient economic size → NO_TRADE, stable reason codes)
- Section 4 enumerates width/sign/domain contracts for sqrt_price_x96 (160), sqrt_price_a/b_x96 (160), liquidity (128), liquidityDelta (128 signed), amounts (256), amount0Max/amount1Max (256), tickLower/tickUpper (24), deadline (256), minLiquidity (128), hook_deltas (256 signed), gas_reserve_wei (256), accrued_fees (256), USDG amounts (256)
- Section 5 specifies the cap-bounded closed-form algorithm with explicit below/inside/above formulas (L0, L1, min(L0, L1))
- Section 6 specifies the single named Q64.64 USDG → token boundary with conservative round-down and explicit error conditions
- Section 11 explicitly distinguishes contract bugs (typed exceptions) from market outcomes (structured NO_TRADE)
- Section 13 cross-references G-V4-SIZING-01, G-LIMIT-01, G-GAS-01, ADR-004, ADR-014, R3, R8, R15

### pre-existing-artifacts-preserved — PASS

The pre-existing sizing module, re-export, 55-test suite, and pinned vectors are preserved verbatim; only the new implementation-layer spec doc plus controller-managed bookkeeping changes are introduced by the candidate commit.

Evidence:

- git show 52923f2 (the candidate commit) only modifies 3 files: docs/implement/strategy/V4_POSITION_SIZING.md (new), todo/config.yaml (workflow state transition CHANGES_REQUESTED → AWAITING_REVIEW, attempt 1 → 2), and todo/evidence/P05/T049/attempt-002-developer.json (new developer handoff)
- src/robinhood_lp/protocol/sizing.py (1097 lines), src/robinhood_lp/protocol/__init__.py (147 lines), tests/test_position_sizing_t049.py (1151 lines), and tests/fixtures/strategy/position_sizing_vectors.json (191 lines) appear unchanged in the candidate commit vs attempt-1 planning candidate (verified by git stat showing the same line counts and the absence of these files in the candidate diff stat)
- Protocol __init__.py re-exports the full sizing public surface (BINDING_AMOUNT0, BINDING_AMOUNT1, BINDING_BOTH, BindingSide, MAX_UINT128, MAX_UINT256, POSITION_BELOW/INSIDE/ABOVE, PositionRelative, ReasonCode, SizingDecision, SizingError + 7 typed errors, compute_cap_bounded_liquidity, compute_sizing, get_amounts_for_liquidity, usdg_amount_to_token_amount, worst_case_single_sided_inventory) — confirmed by inspection and by import round-trip from robinhood_lp.protocol

### integer-only-arithmetic — PASS

The sizing module is integer-only (ADR-004). All math is Python `int`; USDG conversion uses Q64.64 fixed-point only. No `float` or `Decimal` enters the integer domain.

Evidence:

- src/robinhood_lp/protocol/sizing.py uses no `float` and no `Decimal` symbols: AST scan returned zero matches; textual scan returns only docstring occurrences on lines 13, 15, 506 (in ``float`` prose), no live identifiers
- All field validators (_require_uint, _require_int, _require_uint160/128, _require_int24) reject non-int / bool / negative / out-of-width inputs with SizingInputError
- _liquidity_for_amount0/_1, _apply_gas_reservation, compute_cap_bounded_liquidity, get_amounts_for_liquidity, usdg_amount_to_token_amount all operate on Python `int` only (multiplications, shifts, floor division)
- usdg_amount_to_token_amount implements (usdg_amount << 64) // price_q64_64 — pure integer Q64.64 fixed-point with conservative round-down; rejects non-positive price (SizingUsdgConversionError) and overflow (SizingUsdgConversionError)
- Test test_protocol_module_has_no_float inspects sizing_module globals for float values and asserts no float symbols exist
- mypy --strict src/ passes (Success: no issues found in 86 source files)

### single-canonical-liquidity-delta — PASS

The sizer enforces one canonical integer liquidityDelta per position; per-side trimming is impossible by construction (no per-side liquidityDelta0/1 fields, single binding-side label).

Evidence:

- SizingDecision carries a single `liquidity` field (uint128), not per-side liquidityDelta0/liquidityDelta1
- compute_sizing returns one tuple `(liquidity, binding_side)` from compute_cap_bounded_liquidity, and that single liquidity is used for both amount0 and amount1 via get_amounts_for_liquidity — there is no per-side trimming path
- Hard invariant I2 in the spec doc states: 'One liquidity scale. The whole position shares a single liquidityDelta; per-side trimming is forbidden'
- Tests test_compute_sizing_ok_inside_range, test_compute_sizing_ok_below_range_binds_amount0, and test_compute_sizing_ok_above_range_binds_amount1 all assert that the binding side is correctly labelled and liquidity is single-valued
- test_lowering_caps_never_changes_ticks asserts the Range inputs are not mutated by the cap-bounded step

### range-not-modified — PASS

The Range is never widened, narrowed, shifted, or re-aligned to satisfy a cap. The Range is not an output of any sizing step.

Evidence:

- compute_sizing only reads tick_lower / tick_upper (and the pool's tick_spacing for alignment); it never writes or returns tick values
- tick_lower >= tick_upper yields a structured NO_TRADE / RANGE_DEGENERATE (not a Range change); ticks not aligned to tick_spacing raise SizingAlignmentError (contract bug)
- compute_cap_bounded_liquidity returns only (liquidity, binding_side); pa and pb are inputs only
- test_lowering_caps_never_changes_ticks captures (pa_before, pb_before) before compute_cap_bounded_liquidity and asserts they are unchanged afterwards
- Hard invariant I1 and I7 in the spec doc: Range is fixed; Range is not moved to fit a cap

### no-independent-side-trim — PASS

No independent side-trim path exists. The whole position uses a single liquidity value.

Evidence:

- The algorithm uses min(L0, L1) — a single shared liquidity; there is no code path that solves for liquidity on one side independently and then trims the other side
- SizingDecision has one liquidity field; there is no per-side liquidity_delta field
- compute_cap_bounded_liquidity does not accept a 'skip_amount0' / 'skip_amount1' flag that would allow asymmetric trimming
- Hard invariant I2 in the spec doc forbids per-side trimming; Must-not clause 'independently trim one side' is satisfied

### gas-reserve-preserved — PASS

The Gas reserve is subtracted from the native-side cap once and is preserved. The reserve is never spent on the LP itself; an underflow produces NO_TRADE / GAS_RESERVE_UNDERFLOW rather than allowing the LP to proceed.

Evidence:

- _apply_gas_reservation subtracts gas_reserve_wei from the spendable cap of whichever side is native (Currency.native(), Address(0)); the other side is untouched
- Reserve underflow (cap < gas_reserve_wei) returns -1 sentinel, which compute_sizing maps to NO_TRADE / GAS_RESERVE_UNDERFLOW — never an exception that would let the LP proceed with the reserve spent
- compute_sizing never calls payable{value: ...} and never spends Gas on the LP itself (G-GAS-01)
- Test test_native_gas_reservation_subtracts_once_from_cap verifies cap0_max == cap0 - gas_reserve when currency0 is native
- Test test_native_gas_reservation_zero_when_not_native verifies the reserve is ignored when neither currency is native
- Test test_native_gas_reserve_underflow_returns_no_trade verifies the underflow path returns NO_TRADE
- Test test_native_gas_reservation_exact_match verifies cap == reserve → effective cap == 0 → NO_TRADE (NOT silent spend)

### hook-balance-delta-verified — PASS

Hook BalanceDelta is integer, requires explicit verification (hook_verified=True), and is folded into required_amount* before the cap check. Unverified deltas raise; verified charges are funded from the cap; verified credits reduce the required amount; over-credits are rejected.

Evidence:

- compute_sizing rejects non-zero (hook_delta0, hook_delta1) with hook_verified=False by raising SizingHookError (typed contract-bug exception), preventing silent proceeds
- When hook_verified=True: required_amount0 = amount0 + hook_delta0 (and same for side 1); a positive delta (charge) is funded from the wallet, a negative delta (credit) reduces the required amount
- A hook credit that would push required_amount below zero raises SizingHookError (contract bug)
- Post-hook required amounts are checked against effective_amount_caps; a charge that exceeds the cap yields NO_TRADE / CAP_UNDERFLOW (must-not unprotected delta-derived Mint)
- Tests test_compute_sizing_unverified_hook_raises, test_compute_sizing_verified_hook_charge_funded, test_compute_sizing_verified_hook_charge_exceeds_cap_returns_no_trade, test_compute_sizing_verified_hook_credit_reduces_required, and test_compute_sizing_hook_credit_negative_required_raises all pass

### usdg-q64-64-conversion — PASS

The USDG → raw-token conversion is the single named numeraire boundary, implemented with integer Q64.64 fixed-point arithmetic, conservative round-down, and pinned vector coverage. No float/Decimal is introduced.

Evidence:

- usdg_amount_to_token_amount(usdg_amount, price_q64_64) implements (usdg_amount << 64) // price_q64_64 — pure integer Q64.64, conservative round-down
- Section 6 of the spec doc states this is the single named numeraire boundary; no float/Decimal enters
- Pinned vectors in usdg_conversion cover 1:1 (1000000000000000000), round-down (10 / 3 * 2^64 → 3), price-doubles-token-halves (2x → 0.5x), and a 5e17 sanity check; all match byte-exactly (verified by direct call and by pytest parametrized test_usdg_conversion_match_pinned)
- Test test_usdg_conversion_rounds_down uses 2^63 usdg @ 3 * 2^64 price and asserts (2^63 // 3) result
- Errors: non-positive price → SizingUsdgConversionError; uint256 overflow → SizingUsdgConversionError
- The sizer itself never receives a USDG amount on the cap path; only the caller converts USDG → token before passing amount0_cap / amount1_cap

### pinned-vectors-cover-rounding-and-boundary — PASS

Pinned Solidity-derived vectors cover rounding (round-down, halving, ten-per-three) and tight boundary prices (±60 range). The Python module reproduces each vector byte-exactly.

Evidence:

- tests/fixtures/strategy/position_sizing_vectors.json pins 18 vector instances across 7 vector groups: below_range_amounts (2), above_range_amounts (2), inside_range_amounts (2), tick_boundary_sizes (1, tight ±60 range), worst_case_boundary_inventory (2), cap_bounded_liquidity (5), usdg_conversion (4)
- All 18 pinned vectors match byte-exactly when reproduced by the sizer (verified by direct Python invocation matching the pytest-parametrized tests)
- Coverage includes rounding (round_down_ten_per_three 10 / 3 * 2^64 → 3) and tight boundary ticks (±60 tick range with liquidity 5e17 — explicit boundary-price vector)
- Vectors declare pinned_commit e50237c43811bd9b526eff40f26772152a42daba in tools/oracle/lib/v4-core; they were produced by re-running the T012 Python port of V4 integer math (LiquidityAmounts.getAmountsForLiquidity / getLiquidityForAmount0 / getLiquidityForAmount1 / SqrtPriceMath.getAmount0Delta / getAmount1Delta)
- All 55 tests in tests/test_position_sizing_t049.py pass: 55 passed in 0.13s

### monotonicity-lowering-cap-only-reduces-liquidity — PASS

Lowering either approved cap can only reduce or hold liquidity, never increase it. Ticks are never moved by the cap-bounded step.

Evidence:

- test_lowering_amount0_cap_only_reduces_liquidity computes (pa, pb) at ±100 ticks, big=10^20; halves amount0_cap and asserts liq_half <= liq_full and binding side is amount0 or amount1 (never both absent)
- test_lowering_amount1_cap_only_reduces_liquidity does the symmetric assertion for amount1_cap
- test_compute_sizing_lowering_cap_only_reduces_liquidity runs the same check at the full compute_sizing level
- Closed-form per-side inverses guarantee monotonicity: L0 = amount0 * pa * p / ((p - pa) << 96) is monotone non-decreasing in amount0; L1 = amount1 * Q96 / (pb - p) is monotone non-decreasing in amount1; min(L0, L1) is therefore monotone non-decreasing in each input
- Empirically: at tick ±100, sqrt_price_for_tick_0, big=10^24, full=199510416479002803287822012, half amount0=99755208239501401643911006, half amount1=99755208239501401643911006 — exactly half, monotone

### post-settlement-respects-both-caps — PASS

Final simulated settlement stays below both raw-token caps after Hook and Gas effects. The inside-branch formula uses the actual brackets to guarantee the round-trip respects the caps.

Evidence:

- compute_sizing computes post_amount0 = effective_amount0_cap + accrued_fees0 - required_amount0 and post_amount1 analogously; both must be >= 0 or the result is NO_TRADE / CAP_UNDERFLOW
- Inside-branch formulas use the actual brackets [pa, p] for token0 and [p, pb] for token1 (not V4's asymmetric reference brackets), so the round-trip amounts_for_liquidity ∘ liquidity_for_amounts respects both supplied caps
- Hook deltas are folded into required_amount* before the post-settlement check (must-not ignore Hook deltas)
- Gas reservation is preserved by construction (subtracted from the spendable cap once before the cap-bounded step)
- test_simulated_settlement_respects_both_caps asserts amount0_max + 0 >= amount0 + 0 and amount1_max + 0 >= amount1 + 0
- test_native_gas_reservation_subtracts_once_from_cap verifies the Gas-reservation-adjusted caps equal the post-settlement envelope
- Hard invariant I4 in the spec doc: both raw-token caps are respected after Hook and Gas effects

### insufficient-economic-size-no-trade — PASS

Insufficient economic size returns NO_TRADE with ReasonCode.INSUFFICIENT_ECONOMIC_SIZE.

Evidence:

- When min_economic_liquidity_usdg_q64_64 is supplied and at least one non-zero usdg_amount_token* is supplied, the sizer computes a conservative Q64.64 USDG-equivalent of the sized liquidity and returns NO_TRADE / INSUFFICIENT_ECONOMIC_SIZE when the equivalent is below the minimum
- test_insufficient_economic_size_returns_no_trade passes with min_economic_liquidity_usdg_q64_64 = 1 << 80 (deliberately too large) → NO_TRADE
- test_minimum_economic_size_passes_when_satisfied passes with min_economic_liquidity_usdg_q64_64 = 1 → OK

### no-float-or-display-in-sizing-math — PASS

The sizing math does not use float or display values. Only integer Q64.64 fixed-point arithmetic is used on the protocol/accounting path.

Evidence:

- AST scan: zero references to `float` or `Decimal` identifiers in src/robinhood_lp/protocol/sizing.py
- Textual scan: only docstring occurrences (lines 13, 15, 506) — prose about what the module avoids
- my_test_protocol_module_has_no_float inspects the module's public globals for float values and asserts each POSITION_*/BINDING_* sentinel is a str (not a float)
- mypy --strict src/ passes

### fifty-five-tests-green — PASS

All 55 T049 sizing tests pass and the full suite (excluding Foundry/gpg/skipped tests) is green.

Evidence:

- python -m pytest tests/test_position_sizing_t049.py -q → 55 passed in 0.13s (37 non-parameterized + 18 parameterized instances, total 55)
- Full suite (excluding test_workflow.py / test_abi_artifacts.py per attempt-2 developer evidence) → 1351 passed, 6 skipped in 12.49s; the 6 skips are Foundry-conditional (forge not on PATH), gpg-verification-out-of-scope, and an unsorted-currencies vector — same as base

### ruff-and-mypy-clean — PASS

ruff format, ruff check, and mypy --strict are all clean.

Evidence:

- ruff format --check src/ tests/ docs/implement/ → 154 files already formatted
- ruff format --check docs/implement/strategy/V4_POSITION_SIZING.md → 1 file already formatted
- ruff check src/ tests/ docs/implement/ → All checks passed!
- mypy --strict src/ → Success: no issues found in 86 source files

### no-protected-path-violation — PASS

The candidate commit introduces a single Developer-authorable change (docs/implement/strategy/V4_POSITION_SIZING.md). The todo/config.yaml and todo/phases/... entries in the candidate-vs-base diff are controller-managed transitions (the planning amendment approved by plan-review-001 and the workflow_state stamp written by finish-develop). No Developer write violates PROTECTED_PREFIXES.

Evidence:

- git show 52923f2 (the candidate commit) modifies exactly 3 files: docs/implement/strategy/V4_POSITION_SIZING.md (new, NOT under PROTECTED_PREFIXES — docs/implement/ is Developer-authorable per tools/workflow/core.py PROPHET_EDITABLE rule and outside PROTECTED_PREFIXES = ('.claude/', 'docs/intent/', 'docs/spec/', 'todo/phases/', 'todo/schemas/', 'tools/workflow/')), todo/config.yaml (controller-managed workflow_state transition CHANGES_REQUESTED → AWAITING_REVIEW + attempt 1 → 2 written by finish-develop), and todo/evidence/P05/T049/attempt-002-developer.json (controller-managed developer handoff file written by finish-develop)
- The developer-side diff (sizing.py, __init__.py, test file, fixture, T049.md) was created and approved on prior planning/triage commits (4e70486 chore(planner), 69f8495 docs(t049) planning candidate, bd8cbcb chore(workflow) record plan review) and is unchanged in the candidate commit; the candidate commit preserves these files verbatim
- tools/workflow check-paths base_commit reports the diff vs base contains todo/config.yaml and todo/phases/... changes; these are controller-managed bookkeeping transitions (planning relocation from attempt-1 plan-review-001 PASS and the workflow_state/attempt stamp written by finish-develop), not Developer writes — the controller's prepare_develop→finish_develop snapshot mechanism enforces no Developer writes between snapshot and seal
- No file under docs/spec/, docs/intent/, tools/workflow/, .claude/, todo/schemas/, CLAUDE.md, AGENTS.md, or todo/README.md is touched by the Developer's actual delta; only docs/implement/strategy/V4_POSITION_SIZING.md is the Developer-introduced change

### intent-spec-task-contract-controller-untouched — PASS

Intent, Spec, task-contract (other than the planned Deliverable relocation), workflow controller, and configuration files outside todo/config.yaml's workflow_state stamp are untouched.

Evidence:

- The candidate commit does not modify docs/intent/, docs/spec/, tools/workflow/, .claude/, todo/schemas/, AGENTS.md, CLAUDE.md, or todo/README.md
- todo/phases/P05-features-and-valuation/T049.md is touched only by the controller-managed planning amendment (commit 4e70486 chore(planner) on the branch, approved by plan-review-001 verdict PASS), not by the Developer's attempt-2 candidate
- tools/workflow/cli.py and tools/workflow/core.py are byte-identical to base

### must-not-clauses — PASS

All Must-not clauses are satisfied. Each one is enforced by typed exceptions or structured NO_TRADE verdicts with stable ReasonCode values.

Evidence:

- no float/display values in sizing math: see check 'integer-only-arithmetic' and 'no-float-or-display-in-sizing-math'
- no independent trim: see check 'no-independent-side-trim'
- no Gas reserve spent: see check 'gas-reserve-preserved'
- Hook deltas not ignored: see check 'hook-balance-delta-verified'
- Range not changed to fit a cap: see check 'range-not-modified'
- no unprotected delta-derived Mint: see check 'hook-balance-delta-verified' (SizingHookError raised when hook_verified=False; NO_TRADE / CAP_UNDERFLOW returned when verified charge exceeds cap)

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- Two pre-existing docstrings in src/robinhood_lp/protocol/sizing.py (lines 4 and 672) reference the old path `docs/spec/strategy/V4_POSITION_SIZING.md` even though the relocated implementation spec lives at `docs/implement/strategy/V4_POSITION_SIZING.md`. These are descriptive cross-references, not load-bearing for any test or behavior; they are preserved verbatim per the contract's 'pre-existing artifacts preserved' clause. The new implementation-layer spec doc itself uses the correct path. No contract clause requires the pre-existing module docstrings to be re-pointed.
- The Foundry oracle regenerator at tools/oracle/src/SelectorOracle.sol (exercised by tests/test_abi_artifacts.py) depends on the git submodule at tools/oracle/lib/v4-core (pinned commit e50237c43811bd9b526eff40f26772152a42daba). The submodule is not populated in this worktree, so Foundry-conditional tests skip; the 6 skips in the full suite are all Foundry/gpg/unsorted-currency conditional skips that also exist on base. The pinned V4 vectors used by the sizing tests are the byte-equal Python re-derivation from the T012 integer math port; regenerating the Foundry oracle is out of T049's scope.
- The insufficient-economic-size USDG gate uses a linear-projection estimate (`_liquidity_to_usdg_equivalent`) rather than a closed-form valuation. This is a conservative Q64.64 estimate sufficient for the NO_TRADE gate; the full USDG valuation belongs to T053 per the contract's references. If T053 later defines a stricter USDG-equivalent rule, the gate will need to be re-aligned without changing the integer discipline.
- The closed-form per-side inverses inside `compute_cap_bounded_liquidity` guarantee monotone non-decreasing liquidity in each cap (by inspection of L0 = amount0 * pa * p / ((p - pa) << 96) and L1 = amount1 * Q96 / (pb - p)). The structural test `test_lowering_caps_never_changes_ticks` plus the two cap-monotone tests cover the contract clause empirically. No formal proof is included in the spec doc; the empirical coverage matches the contract's acceptance language.
