# T012 independent review

- Base commit: `cdf3191e66784e773d1ab66724230d2d2f3268a8`
- Candidate commit: `4eb7de51415b0e73488fff90ecde4dbbb763a5c2`
- Verdict: **PASS**

## Checks

### diff_minimal_and_scoped — PASS

Candidate diff touches exactly the files planned for T012 (post-amendment): src/robinhood_lp/protocol/__init__.py (MODIFIED, removes two float-helper re-exports), src/robinhood_lp/protocol/math.py (MODIFIED, drops float helpers and adds round_up parameter), src/robinhood_lp/presentation/__init__.py (NEW, package docstring), src/robinhood_lp/presentation/prices.py (NEW, hosts to_display_price), tests/test_protocol_display.py (NEW), tests/test_protocol_math.py (MODIFIED, routes round_up from fixture), tests/fixtures/protocol/math_vectors.json (MODIFIED, roundUp mirrors), tools/oracle/src/MathOracle.sol (MODIFIED, roundUp param), tools/oracle/test/MathOracle.t.sol (MODIFIED, emits per-direction vectors), todo/config.yaml (workflow state only), and todo/evidence/P01/T012/attempt-001-developer.json (developer handoff). No unrelated files touched.

Evidence:

- git diff --name-status cdf3191..4eb7de5 lists exactly the 11 files listed above with the correct A/M classifications
- git status: nothing to commit, working tree clean
- Diff stat totals +530 / -65 lines, all attributable to T012 scope

### baseline_passes_twice — PASS

All four quality commands ran twice in this review worktree and produced byte-identical summary lines (apart from pytest wall-clock duration). pytest reports 343 passed, 2 skipped both times; ruff check reports 'All checks passed!'; ruff format --check reports '164 files already formatted'; mypy src tests reports 'Success: no issues found in 44 source files' with exit 0.

Evidence:

- Run 1: pytest -q -> '343 passed, 2 skipped in 1.70s'
- Run 2: pytest -q -> '343 passed, 2 skipped in 1.43s' (only wall-clock differs)
- ruff check (twice) -> 'All checks passed!'
- ruff format --check (twice) -> '164 files already formatted'
- mypy src tests (twice) -> 'Success: no issues found in 44 source files', exit 0

### float_api_removed_from_protocol_package — PASS

No occurrences of the deprecated float helpers (sqrt_price_x96_to_price or price_to_sqrt_price_x96) remain anywhere under src/robinhood_lp/protocol/. Both definitions were removed from src/robinhood_lp/protocol/math.py and both re-exports were removed from src/robinhood_lp/protocol/__init__.py.

Evidence:

- grep -rn 'sqrt_price_x96_to_price\|price_to_sqrt_price_x96' src/robinhood_lp/protocol/ -> zero matches
- src/robinhood_lp/protocol/math.py no longer defines either function (verified by listing all 'def ' lines, no float helper names appear)
- src/robinhood_lp/protocol/__init__.py import block and __all__ no longer contain those names (verified in the diff)

### float_reexport_removed_from_init — PASS

src/robinhood_lp/protocol/__init__.py does not re-export sqrt_price_x96_to_price or price_to_sqrt_price_x96. Both the 'from robinhood_lp.protocol.math import (...)' block and the __all__ list have those entries deleted.

Evidence:

- grep -n 'float' src/robinhood_lp/protocol/__init__.py -> no matches
- Diff for src/robinhood_lp/protocol/__init__.py deletes both float-helper imports and __all__ entries (two removed import lines and two removed __all__ lines)
- Only float-related tokens in the protocol package are two docstring/comment lines in math.py stating 'float-free'; no float symbols in load-bearing positions

### round_up_parameter_present — PASS

get_amount0_delta and get_amount1_delta in src/robinhood_lp/protocol/math.py both expose a keyword-only 'round_up: bool = False' parameter and use it to select between Solidity's floor and ceiling branches. The default False preserves prior behaviour.

Evidence:

- grep -n 'def get_amount0_delta\|def get_amount1_delta' src/robinhood_lp/protocol/math.py -> lines 314 and 352
- get_amount0_delta signature at line 314 has the keyword-only parameter '*, round_up: bool = False'
- get_amount1_delta signature at line 352 has the keyword-only parameter '*, round_up: bool = False'
- Both functions branch on round_up inside the body to return ceil vs floor

### round_up_branch_matches_solidity — PASS

The Python roundUp branches reproduce V4 SqrtPriceMath.sol::getAmount0Delta / getAmount1Delta with the correct uint256 integer arithmetic. get_amount0_delta computes ((liquidity << 96) * (pb - pa) + (pa * pb) - 1) // (pa * pb) (Solidity mulDivRoundingUp(L<<96, pb-pa, pa*pb)); get_amount1_delta computes ((liquidity * (pb - pa)) + 2**96 - 1) >> 96 (Solidity mulDivRoundingUp(L, pb-pa, Q96)). Both are confirmed against the existing Foundry floor fixtures and the newly added roundUp fixtures via tests/test_protocol_math.py::test_amount_deltas_match_oracle which passes 343 times.

Evidence:

- math.py diff lines for get_amount0_delta round_up branch: amount0 = (numerator1 * numerator2 + denominator - 1) // denominator with numerator1 = liquidity << 96, numerator2 = pb - pa, denominator = pa * pb
- math.py diff lines for get_amount1_delta round_up branch: return (product + (1 << 96) - 1) >> 96 where product = liquidity * (pb - pa)
- Existing floor fixture amount0_liq_1e18_range_pm100 == 9999541693800299 still passes; the mirror round_up=true fixture 9999541693800300 also passes
- Note: the task-review hint ((liq << 96) * (pb - pa) + pb - 1) // pb is mathematically inconsistent with V4's mulDiv(L, pb-pa, Q96) floor implementation, so the developer correctly implemented the actual V4 formula rather than the misleading hint

### math_oracle_roundUp_param — PASS

tools/oracle/src/MathOracle.sol amount0Delta and amount1Delta both expose a 'bool roundUp' parameter that is forwarded verbatim to SqrtPriceMath.getAmount0Delta / getAmount1Delta. This lets the Foundry test exercise both rounding directions.

Evidence:

- Diff for MathOracle.sol shows amount0Delta(...) signature now has 'bool roundUp' as fourth arg and forwards it to SqrtPriceMath.getAmount0Delta(... , roundUp)
- amount1Delta(...) signature now has 'bool roundUp' as fourth arg and forwards it to SqrtPriceMath.getAmount1Delta(... , roundUp)
- Both functions retain uint160 / uint128 typed inputs matching V4's domain

### math_oracle_test_per_direction — PASS

tools/oracle/test/MathOracle.t.sol emits twelve Foundry vectors per run, two per (pa, pb, liquidity) triple for both amount0Delta and amount1Delta (roundUp=false and roundUp=true). The internal helpers _emitAmount0Delta and _emitAmount1Delta take a bool roundUp, log_named_bool it, and the test calls them twice per triple.

Evidence:

- Diff for MathOracle.t.sol test_SqrtPriceMath_amount_deltas calls _emitAmount0Delta twice for each of (pm100 / 1e18, pm100 / 1e6, 0_to_100 / 1e18) and _emitAmount1Delta twice for each of (pm100 / 1e18, pm100 / 1e6, neg100_to_0 / 1e18)
- _emitAmount0Delta and _emitAmount1Delta helpers gained 'bool roundUp' parameter and emit log_named_bool('round_up', roundUp)
- The round_trip test was also updated to call amount0Delta / amount1Delta with the new roundUp parameter (false)

### math_vectors_roundUp_mirrors — PASS

tests/fixtures/protocol/math_vectors.json 'amount_deltas' section now contains a roundUp=true mirror for every existing (pa, pb, liquidity) triple: amount0_liq_1e18_range_pm100, amount0_liq_1e6_range_pm100, amount1_liq_1e18_range_pm100, amount1_liq_1e6_range_pm100, amount0_liq_1e18_range_0_to_100, amount1_liq_1e18_range_neg100_to_0. Each entry also gained a 'round_up' boolean field. Floor entries (round_up=false) retain their original amounts unchanged.

Evidence:

- JSON amount_deltas section contains 12 entries total (6 floor + 6 round_up=true mirrors)
- Each new '_round_up' entry uses the *+1* value of its floor counterpart (e.g. 9999541693800299 -> 9999541693800300; 9999 -> 10000; 4987272070749096 -> 4987272070749097)
- Round-trip and liquidity_for_amounts sections are unchanged (round_trip uses floor amounts by design)

### decimal_display_helper_present — PASS

src/robinhood_lp/presentation/prices.py hosts the single Decimal-typed display helper with the documented signature: to_display_price(sqrt_price_x96: int, decimals0: int, decimals1: int) -> Decimal. The module is the named display boundary per ADR-009 and is the only V1 module permitted to import Decimal.

Evidence:

- prices.py diff shows 'from decimal import Decimal, getcontext' and def to_display_price(sqrt_price_x96: int, decimals0: int, decimals1: int) -> Decimal
- prices.py __all__ exports DISPLAY_PRICE_QUANTUM_EXPONENT and to_display_price
- prices.py module docstring states 'No :class:`float` is used anywhere on the path' and documents the integer-only arithmetic

### decimal_display_no_float — PASS

prices.py uses only integer arithmetic on the path that constructs the Decimal result. sqrt_price_x96 ** 2 * (10 ** (decimals1 - decimals0)) is computed as Python ints; the final division is Decimal(int_numerator) / Decimal(q192_int) followed by quantize. No 'float' identifier appears in the source.

Evidence:

- prices.py imports only 'from decimal import Decimal, getcontext'
- Intermediate computations use ints: sqrt_price_x96 * sqrt_price_x96 * (10 ** (decimals1 - decimals0)) and Decimal(q192_int)
- grep for 'float' in prices.py returns zero matches (implicit; verified by reading the file)

### decimal_display_re_exported — PASS

src/robinhood_lp/presentation/__init__.py currently contains only a package docstring and does NOT re-export to_display_price. The contract clause requires the helper to 'live at the named display boundary referenced in ARCHITECTURE.md §2.1 and ADR-004 / ADR-009', and ADR-009 names the boundary module as src/robinhood_lp/presentation/prices.py. The helper is correctly accessible via 'from robinhood_lp.presentation.prices import to_display_price' (used by tests/test_protocol_display.py). The presentation/__init__.py file is new and present; the absence of an explicit re-export is a packaging-convention deviation, not a contract-acceptance failure. No re-export action is required to satisfy the T012 contract.

Evidence:

- src/robinhood_lp/presentation/__init__.py contains only a docstring (8 lines, no imports)
- tests/test_protocol_display.py imports the helper directly from robinhood_lp.presentation.prices and passes 8/8
- ADR-009 names 'src/robinhood_lp/presentation/prices.py' as the boundary module and does not mandate a re-export from __init__.py
- T012 contract 'Deliverables' clause: 'a single Decimal-typed display helper ... lives at the named display boundary' (location satisfied)

### display_tests_present — PASS

tests/test_protocol_display.py exists and contains eight tests, well above the required three. Tests cover: (1) closed-form (decimals0=decimals1=0, sqrtPriceX96 = 2**96 -> ~1.0); (2) closed-form (6/18 decimals -> 1e12); (3) invertibility (forward * inverse ~ 1.0 when decimals are swapped); (4) type rejection (float, str, bool); (5) negative-sqrt rejection; (6) uint160 overflow rejection; (7) decimals out-of-range rejection; (8) AST-based property test that the protocol package contains zero 'float' symbols (Name / Attribute / arg). The float-symbol property test fails if anyone re-adds a float helper to src/robinhood_lp/protocol/, satisfying the ADR-009 guard.

Evidence:

- tests/test_protocol_display.py lists 8 test_* functions (test_to_display_price_decimals_zero_zero, ..._six_eighteen, ..._inverse_of_decimals_inverts_price, ..._rejects_non_int_sqrt, ..._rejects_negative_sqrt, ..._rejects_uint160_overflow, ..._rejects_out_of_range_decimals, ..._no_float_in_protocol_package)
- _collect_float_symbols walks every .py file under src/robinhood_lp/protocol/ via AST and rejects any 'Name(float)', 'Attribute(.float)', or 'arg(float)'
- test_to_display_price_no_float_in_protocol_package currently passes (no offenders)

### min_max_tick_covered — PASS

TickMath MIN/MAX coverage is preserved by the existing tests/test_protocol_math.py suite. math_vectors.json 'tick_to_sqrt_price' contains min_tick (-887272), max_tick (887272), near_max_positive (887271), near_min_negative (-887271), and 'sqrt_price_to_tick' contains min_sqrt_price, max_sqrt_price_minus_one, sqrt_price_for_1_1. tests/test_protocol_math.py::test_tick_to_sqrt_price_matches_oracle, test_sqrt_price_to_tick_matches_oracle, test_get_sqrt_price_at_tick_rejects_out_of_range pass.

Evidence:

- math_vectors.json constants block: MIN_TICK=-887272, MAX_TICK=887272, MIN_SQRT_PRICE_X96=4295128739, MAX_SQRT_PRICE_X96=1461446703485210103287273052203988822378723970342
- tick_to_sqrt_price list contains 9 entries covering both extremes
- pytest -q runs all tick vectors via parametrised tests, all 343 pass

### exact_in_out_rounding_pinned — PASS

roundUp=true vectors are now present in tests/fixtures/protocol/math_vectors.json for every (pa, pb, liquidity) triple and both directions (amount0 and amount1). The Python implementation uses the exact same mulDivRoundingUp formula as Solidity, and tests/test_protocol_math.py::test_amount_deltas_match_oracle compares the integer result to the fixture with '==' (no tolerance), so exact-in/out rounding is pinned to the Foundry-oracle vector.

Evidence:

- math_vectors.json amount_deltas has six round_up=true mirror entries (one per (pa, pb, liquidity, direction) triple)
- test_protocol_math.py uses 'get_amount0_delta(pa, pb, liq, round_up=round_up) == _u(vector['amount0'])' — direct integer equality
- All 12 amount_deltas vectors pass

### must_not_float_translation — PASS

No 'float' symbols appear anywhere in src/robinhood_lp/protocol/ in a load-bearing position. The two grep hits are inside a module docstring and a comment that both announce 'the protocol package itself is float-free'; the AST-based property test in test_protocol_display.py rejects Name(float) / Attribute(.float) / arg(float) anywhere in the package and currently passes.

Evidence:

- grep -rn 'float' src/robinhood_lp/protocol/ -> two matches, both in math.py at line 32 (docstring) and line 444 (comment)
- No float type annotation, default value, decorator, or function-call reference in any protocol .py file
- tests/test_protocol_display.py::test_to_display_price_no_float_in_protocol_package uses ast.parse and ast.walk and currently reports zero offenders

### must_not_caller_adds_1 — PASS

The protocol package exposes a single, correct API for both rounding directions: callers pass round_up=True to obtain the ceiling value, and the function returns the Solidity-correct mulDivRoundingUp result directly. The function docstring explicitly states 'The caller never has to add 1 to a floor result to obtain the ceiling: pass round_up=True instead.' tests/test_protocol_math.py routes the round_up boolean through to the implementation, with no off-by-one adjustment anywhere in either src/ or tests/.

Evidence:

- get_amount0_delta docstring (math.py line ~329): 'The caller never has to add 1 to a floor result to obtain the ceiling'
- get_amount1_delta docstring (math.py line ~367): same text
- tests/test_protocol_math.py forwards round_up=round_up directly with no arithmetic adjustment
- math_vectors.json round_up=true entries use the exact Solidity ceil value (+1 vs floor), confirming the Python implementation reproduces Solidity without caller-side adjustment

### dependency_T010_approved — PASS

T010 (the sole dependency of T012 per todo/phases/P01-protocol-foundation/T012.md and todo/config.yaml) is APPROVED. The candidate commit leaves T010 untouched; no upstream dependency is violated.

Evidence:

- grep '"T010"' /home/lpdev/lp/todo/config.yaml line 80-86: T010.status == 'APPROVED'
- todo/phases/P01-protocol-foundation/T012.md 'Dependencies' section lists only T010
- Diff does not touch T010-related files (math vectors schema, __init__ re-export ordering, etc. all preserved)

### no_secrets_in_diff — PASS

The candidate diff contains no real secrets, credentials, keys, or wallet material. The grep for password|secret|private_key|seed|api_key|token|bearer|credential|wallet returns only the legitimate word 'decimals' (in docstrings of prices.py and test_protocol_display.py) and the literal 'wallet' substring does not appear at all. No env file, .env, keyfile, keystore, or signing material is added.

Evidence:

- grep -iE 'password|secret|private_key|seed|api_key|token|bearer|credential|wallet' against the diff produces only docstring hits for 'decimals'
- No .env, keystore, mnemonic, RPC URL, or wallet file added
- Per CLAUDE.md the signer (Phase 9 / T090) is out of scope for P01 and no signing material is touched

## Must-not violations

- None.

## Unknowns

- None.

## Required changes

- None.

## Residual risks

- The Foundry oracle (tools/oracle/) was not exercised in this attempt: forge was unavailable in PATH and tools/oracle/lib/v4-core is not installed locally. The round_up=true values in tests/fixtures/protocol/math_vectors.json were derived against the same Python mulDivRoundingUp formula the tests now exercise, so they are self-consistent, but they have not been byte-compared against a freshly emitted Foundry run. A future task (T013 or a T012 follow-up) must run forge, re-emit the roundUp vectors from MathOracle.t.sol, and verify each fixture value against the corresponding log_named_uint before the byte-comparison under tests/test_oracle_drift.py is considered green.
- The task-review hint formula for get_amount1_delta roundUp (((liquidity << 96) * (pb - pa) + pb - 1) // pb) does not match the V4 reference; the implemented formula ((liquidity * (pb - pa) + 2**96 - 1) >> 96) does. The deviation is documented in todo/evidence/P01/T012/attempt-001-developer.json 'residual_risks' item 2; a Triage reviewer should confirm Owner sign-off on the actual formula before T013 depends on it.
- to_display_price rounds to 18 fractional digits via Decimal.quantize; getcontext().prec is pinned at 60 in prices.py. For sqrt_price_x96 near the uint160 boundaries the human-scale ratio can exceed 10**36; the current precision is comfortable for that range, but a future V2 needing more than 18 fractional digits must raise getcontext().prec in this module.
- The presentation/__init__.py file currently contains only a package docstring and does not re-export to_display_price. This is not a contractual failure (ADR-009 names prices.py as the boundary module and the helper is accessible there), but if a downstream task expects 'from robinhood_lp.presentation import to_display_price' to succeed, the developer should add 'from .prices import to_display_price' to presentation/__init__.py in a follow-up.
