# Oracle Manifest

This document records the exact source revisions used to generate
the pinned protocol vectors under `tests/fixtures/protocol/`. It is
the T013 provenance record.

## Vectors produced by this oracle

| Vector file | Task | Vectors | Edge classes covered |
| --- | --- | --- | --- |
| `tests/fixtures/protocol/pool_id_vectors.json` | T010 | 7 | native currency, static fee, dynamic fee, max static fee, max tick spacing, nonzero hooks, reordered inputs |
| `tests/fixtures/protocol/math_vectors.json` | T012 | 21 | MIN/MAX tick, zero, ±100, ±100000, near_min/near_max; MIN/MAX sqrt price; amount0/amount1 across ranges and one-sided; liquidity round-trip |

## Generator environment

- **Generator:** Foundry
  - `forge` 1.8.1 (commit 982849d314 2026-08-28T17:46:00Z; user-level install
    under `~/.foundry/bin`)
- **Solidity compiler:** 0.8.26 (`foundry.toml` `[profile.default].solc_version`)
- **Python port (oracle consumer):** CPython 3.12.14
- **Python test runner:** pytest 9.1.1
- **Python keccak256:** `eth-hash[pycryptodome]` 0.8.0 (pycryptodome 3.23.0)

## Pinned source revisions

### v4-core-periphery-submodule

- **Repository:** <https://github.com/Uniswap/v4-core>
- **Pinned commit:** `59d3ecf53afa9264a16bba0e38f4c5d2231f80bc`
  ("bump to 1.0.2", PR #972)
- **Used by:** `LiquidityAmounts.sol` via v4-periphery submodule.

### v4-periphery

- **Repository:** <https://github.com/Uniswap/v4-periphery>
- **Pinned commit:** `dce236d4e2057422d0791d9a973a58765eb46f65`
  ("docs(permissioned-pools): adding perm-pool audit reports", PR #590)
- **Used by:** `LiquidityAmounts.sol`.

### v4-core

- **Repository:** <https://github.com/Uniswap/v4-core>
- **Pinned commit:** `e50237c43811bd9b526eff40f26772152a42daba`
  ("test updates", PR #932)
- **Used by:** `PoolId.sol`, `TickMath.sol`, `SqrtPriceMath.sol`,
  `PoolKey.sol`, `Currency.sol`, `IHooks.sol`.

These three commits are what `forge install` produced on 2026-09-13
when the vectors were first generated. Re-running the oracle against
newer commits may produce different vectors; that is a deliberate
upgrade and must be paired with regenerated JSON fixtures.

## Reproducing the manifest

```bash
export PATH="$PATH:$HOME/.foundry/bin"
cd tools/oracle
forge install --no-commit --commit e50237c43811bd9b526eff40f26772152a42daba Uniswap/v4-core \
              --commit dce236d4e2057422d0791d9a973a58765eb46f65 Uniswap/v4-periphery
git -C lib/v4-core        log -1 --format="%H %s"
git -C lib/v4-periphery    log -1 --format="%H %s"
git -C lib/v4-periphery/lib/v4-core log -1 --format="%H %s"
```

Compare the outputs against the pinned commits above. A mismatch
indicates the upstream repositories have advanced; either update this
manifest and regenerate vectors, or pin a different commit via
`forge install --commit <hash> <repo>`.

## Drift detection

The Python test suite (`tests/test_protocol_ids.py` and
`tests/test_protocol_math.py`) compares the Python port against the
pinned vectors byte-by-byte. If a Solidity change in the oracle
produces different outputs, the corresponding JSON fixture must be
regenerated in the same change set; until then, the Python tests
fail.

In addition, `tests/test_oracle_drift.py` shells out to
`forge test --json -vv` against the pinned submodules and byte-
compares the parsed forge output against the committed JSON
fixtures. When forge is not on PATH (e.g. the T013 sandbox) the test
emits `pytest.skip` with an actionable message; it is **not** a
passing test.

## Manual review

Every edge class in `tests/fixtures/protocol/{pool_id_vectors.json,
math_vectors.json}` carries a per-edge-class entry in
`_meta.reviews`. Each entry records the reviewer identity, the
canonicalized subset of vectors the reviewer claims to have
inspected (`content_sha256`), the ISO-8601 timestamp, and either a
resolvable git SHA (`signature_method: git-author-commit`) or a GPG
key reference (`signature_method: gpg`).

This attempt recorded review metadata using `signature_method: gpg`
(path B in the T013 prompt). The candidate Developer cannot satisfy
the anti-self-attestation rule via `git-author-commit` from this
sandbox; gpg verification is reserved per the contract, and the
reviewer identity is recorded for forward compatibility when gpg
verification is enabled. The empty allowlist at
`tools/reviewers/allowed_signers` is the documented slot for the
gpg key ids that a future attempt may populate.

Verifiers:

- `tests/test_oracle_review_provenance.py` — verifies that every
  edge class has at least one review entry, that every
  `git-author-commit` entry resolves to a real commit with matching
  author email and trailers (`Reviewed-Edge-Class`,
  `Reviewed-Vectors`, `Reviewed-SHA256`), and that the allowlist file
  exists.
- `tools/reviewers/allowed_signers` — the empty allowlist; format
  `<key-id> <principal>` (one per line). gpg verification is
  reserved and the file is intentionally empty in this attempt.

## Foundry project files

- `tools/oracle/foundry.toml` — solc 0.8.26
- `tools/oracle/remappings.txt` — `@v4-core/=lib/v4-core/`,
  `@v4-periphery/=lib/v4-periphery/`
- `tools/oracle/src/{PoolIdOracle,MathOracle}.sol` — oracle wrappers
- `tools/oracle/test/{PoolIdOracle,MathOracle}.t.sol` — vector emitters
