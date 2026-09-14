# V4 Conformance Oracle (T010 / T012 / T013)

This directory holds the **independent Solidity oracle** that generates
pinned V4 protocol vectors for the Python port under
`src/robinhood_lp/protocol/`. It is the canonical T013 conformance
generator: it imports neither the Python implementation nor any of
its own prior outputs.

## Sources

- v4-core: <https://github.com/Uniswap/v4-core>
- v4-periphery: <https://github.com/Uniswap/v4-periphery>
- Foundry: <https://getfoundry.sh> (installed user-level, never sudo)
- Solidity compiler: 0.8.26 (see `foundry.toml`)

The pinned commits live in `lib/v4-core/` and `lib/v4-periphery/` after
`forge install`; they are **not committed** (see `.gitignore`). To
regenerate vectors, re-install with the exact commit pinned in
`docs/implement/evidence/ORACLE_MANIFEST.md`.

## Layout

| File | Purpose |
| --- | --- |
| `foundry.toml` | Foundry config (solc 0.8.26). |
| `remappings.txt` | Maps `@v4-core/` and `@v4-periphery/` to `lib/`. |
| `src/PoolIdOracle.sol` | Wrapper around `PoolId.sol::toId` (T010). |
| `src/MathOracle.sol` | Wrappers around `TickMath.sol`, `SqrtPriceMath.sol`, `LiquidityAmounts.sol` (T012). |
| `test/PoolIdOracle.t.sol` | Emits PoolId vectors (T010). |
| `test/MathOracle.t.sol` | Emits math vectors (T012). |
| `lib/v4-core/`, `lib/v4-periphery/` | Vendored dependencies, git-ignored. |

## Reproducing the vectors

```bash
# one-time
curl -L https://getfoundry.sh | bash   # user-level install to ~/.foundry
export PATH="$PATH:$HOME/.foundry/bin"

cd tools/oracle
forge install Uniswap/v4-core --no-commit
forge install Uniswap/v4-periphery --no-commit

# every time you change the oracle or want to refresh vectors
forge test -vv > /tmp/oracle.log
```

Each test logs field-by-field values for every vector. The exact log
output must be transcribed into `tests/fixtures/protocol/*.json` so
the Python port has byte-exact pinned values to compare against.

## Provenance manifest

The `docs/implement/evidence/ORACLE_MANIFEST.md` file records, for each generated vector
set, the source repository and the exact commit that produced it.
When the oracle or its dependencies are upgraded, the manifest is
updated and the vector JSON files are regenerated in the same change
set.

## Drift test

`tests/test_oracle_drift.py` runs the Python port against the pinned
vectors. If anyone changes the oracle's Solidity source without
regenerating the JSON fixtures, the Python port's output will diverge
and CI will fail at the next run.

## Why an independent oracle

The Python implementation depends on `eth-hash[pycryptodome]` for
keccak256 and a hand-rolled ABI encoder for `PoolKey`. A bug in
either could make the Python port produce self-consistent but wrong
results. The Solidity oracle guarantees keccak256, ABI encoding,
tick math, and liquidity formulas all match the canonical V4
reference implementation.
