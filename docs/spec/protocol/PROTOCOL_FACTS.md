# Protocol Facts — Uniswap V4

> Machine-readable evidence for hard-coded protocol constants and rules used
> by the framework. Each fact records the source path, the file/commit, and
> the retrieval time. Values here may be referenced by code only after the
> source is read; the URL itself is mutable and is **not** the source of truth.

**Retrieval time:** 2026-09-13
**Source repository:** <https://github.com/Uniswap/v4-core>
**Pinned source commit:** `e50237c43811bd9b526eff40f26772152a42daba`
(see the T013 provenance in `docs/implement/evidence/ORACLE_MANIFEST.md`)

## PoolKey

Source: `src/types/PoolKey.sol`

```solidity
struct PoolKey {
    Currency currency0;
    Currency currency1;
    uint24 fee;
    int24 tickSpacing;
    IHooks hooks;
}
```

Invariants:

- `currency0` < `currency1` numerically (compared as `uint160`) is required for a
  canonical initialized pool. `PoolIdLibrary.toId` itself only hashes the supplied tuple
  and does not validate ordering; the framework validates before deriving/accepting it.
- `fee` is a `uint24`. Valid range: `0 .. MAX_LP_FEE` inclusive, or the
  exact sentinel `DYNAMIC_FEE_FLAG`. Any other value reverts.
- `tickSpacing` is a positive `int24` (see TickMath below).
- `hooks` is an `IHooks` (an `address`); see Hook address validity.

## Currency

Source: `src/types/Currency.sol`

- `type Currency is address;` — Currency is a Solidity user-defined value
  type wrapping `address`; no extra bytes.
- Native currency is `Currency.wrap(address(0))`.
- `toId()` returns `uint256(uint160(address))`.

The framework treats `Currency` as `address` (Python `int` of 20 bytes), with
`0` reserved for native.

## LP fee

Source: `src/libraries/LPFeeLibrary.sol`

| Constant | Value | Notes |
| --- | --- | --- |
| `MAX_LP_FEE` | `1_000_000` | Hundredths of a bip; 100%. |
| `DYNAMIC_FEE_FLAG` | `0x800000` | Bit 23 set. Exact equality, not a range. |
| `OVERRIDE_FEE_FLAG` | `0x400000` | Bit 22 set; emitted by `beforeSwap`. |
| `REMOVE_OVERRIDE_MASK` | `0xBFFFFF` | Used to clear the override flag. |

`isDynamicFee(fee)` is `fee == DYNAMIC_FEE_FLAG` (exact equality).

`isValid(fee)` is `fee <= MAX_LP_FEE`.

## Tick range and spacing

Source: `src/libraries/TickMath.sol`

| Constant | Value |
| --- | --- |
| `MIN_TICK` | `-887_272` |
| `MAX_TICK` | `887_272` |
| `MIN_TICK_SPACING` | `1` |
| `MAX_TICK_SPACING` | `32_767` (`type(int16).max`) |

`maxUsableTick(spacing) = (MAX_TICK // spacing) * spacing`.
`minUsableTick(spacing) = truncTowardZero(MIN_TICK / spacing) * spacing`, implemented
in Python as `-((-MIN_TICK) // spacing) * spacing`. Solidity signed division truncates
toward zero, while Python `//` floors a negative quotient; T012 tests the distinction.

## Hook address validity

Source: `src/libraries/Hooks.sol`

`ALL_HOOK_MASK = uint160((1 << 14) - 1)` — the low 14 bits of the address.

Flag bits (each `1 << N`):

| Bit | Flag |
| --- | --- |
| 13 | `BEFORE_INITIALIZE_FLAG` |
| 12 | `AFTER_INITIALIZE_FLAG` |
| 11 | `BEFORE_ADD_LIQUIDITY_FLAG` |
| 10 | `AFTER_ADD_LIQUIDITY_FLAG` |
| 9 | `BEFORE_REMOVE_LIQUIDITY_FLAG` |
| 8 | `AFTER_REMOVE_LIQUIDITY_FLAG` |
| 7 | `BEFORE_SWAP_FLAG` |
| 6 | `AFTER_SWAP_FLAG` |
| 5 | `BEFORE_DONATE_FLAG` |
| 4 | `AFTER_DONATE_FLAG` |
| 3 | `BEFORE_SWAP_RETURNS_DELTA_FLAG` |
| 2 | `AFTER_SWAP_RETURNS_DELTA_FLAG` |
| 1 | `AFTER_ADD_LIQUIDITY_RETURNS_DELTA_FLAG` |
| 0 | `AFTER_REMOVE_LIQUIDITY_RETURNS_DELTA_FLAG` |

`isValidHookAddress(hooks, fee)` rules (quoted):

1. Each `*_RETURNS_DELTA_FLAG` requires the corresponding action flag (e.g.
   `BEFORE_SWAP_RETURNS_DELTA_FLAG` requires `BEFORE_SWAP_FLAG`).
2. If `hooks == address(0)`: the fee **must not** be dynamic.
3. If `hooks != address(0)`: `(uint160(hooks) & ALL_HOOK_MASK) != 0` **or**
   the fee is dynamic.

## PoolId

Source: `src/types/PoolId.sol`

```solidity
function toId(PoolKey memory poolKey) internal pure returns (PoolId poolId) {
    assembly ("memory-safe") {
        // 0xa0 represents the total size of the poolKey struct (5 slots of 32 bytes)
        poolId := keccak256(poolKey, 0xa0)
    }
}
```

`PoolId` is a 32-byte value (a `bytes32`). The Python implementation
hashes the ABI-encoded `PoolKey` with keccak256.

`0xa0` (160 bytes) is the size of the ABI-encoded `PoolKey` struct:
5 fields × 32 bytes per slot. The order of fields is the order
declared in `PoolKey.sol`: `(Currency, Currency, uint24, int24, IHooks)`.

ABI encoding rules used by the Python port:

- each field is padded/extended to exactly 32 bytes;
- `Currency` (an `address`) is encoded as `uint256(uint160(address))`
  → zero-padded 32 bytes;
- `uint24 fee` is encoded as `uint256` → zero-padded 32 bytes;
- `int24 tickSpacing` is encoded as `int256` → sign-extended 32 bytes;
- `address hooks` is encoded as `uint256(uint160(address))` →
  zero-padded 32 bytes (the zero address is the canonical "no hooks").

## Confirmed interpretation

- Whether `currency0 < currency1` is compared as signed or unsigned:
  Solidity `address` is a `uint160`; comparison is unsigned. Framework
  uses `int.from_bytes(addr, "big")` and Python's natural unsigned
  integer ordering.
