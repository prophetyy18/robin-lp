# V4 PoolId Oracle (T010 / T013)

This directory holds the independent Solidity oracle used to generate
pinned V4 PoolId vectors. The Python implementation under
`src/robinhood_lp/protocol/` must match these vectors.

## Source

- v4-core: <https://github.com/Uniswap/v4-core>
- commit: see `lib/v4-core/.git` (pinned by `forge install`)
- Solidity compiler: 0.8.26 (see `foundry.toml`)

## Layout

- `src/PoolIdOracle.sol` — the contract that derives PoolId from a
  `(currency0, currency1, fee, tickSpacing, hooks)` tuple.
- `test/PoolIdOracle.t.sol` — the test that emits the pinned vectors
  via `forge test -vv`.
- `lib/v4-core/` — the v4-core dependency installed by `forge install`.

## Reproducing the vectors

```bash
# one-time
curl -L https://getfoundry.sh | bash   # user-level install to ~/.foundry
export PATH="$PATH:$HOME/.foundry/bin"
cd tools/oracle
forge install Uniswap/v4-core --no-git

# every time you change the oracle or want to refresh vectors
forge test -vv
```

The test logs the field-by-field breakdown of every vector. Compare
the `poolId:` line against the `expected_pool_id` field in
`tests/fixtures/protocol/pool_id_vectors.json`.

## Why an independent oracle

The Python PoolId implementation depends on `eth-hash[pycryptodome]`
for keccak256. A bug in either the ABI encoding or the keccak256
wrapper could make the Python port produce self-consistent but wrong
results. The Solidity oracle guarantees keccak256, ABI encoding, and
PoolId derivation all match the canonical V4 reference implementation.
