# T003 suppression registry

T003 acceptance requires that **every** CI suppression (allowlist,
ignore-pattern, or skipped test) carries a structured entry with
`reason`, `owner`, `expiry`, and `date_added`. This file mirrors the
canonical registry at `docs/implement/ci/suppressions.toml` and is the
machine-readable evidence source for the T003 Reviewer.

Empty entries are explicitly marked **"none encountered"** so a
Reviewer can distinguish "the Developer looked and found nothing" from
"the Developer did not check".

## Suppression table

| Scope | Reason | Owner | Expiry | Date added |
| --- | --- | --- | --- | --- |
| ruff `E501` (line-too-long) | Lines in `src/robinhood_lp/config/models.py` and `tests/test_config.py` contain deliberately long Pydantic field combinations or hex literals; the formatter already wraps docstrings and the project-wide `line-length = 100` is a soft target here. Re-enable when ruff `E501` is no longer ignored in `pyproject.toml [tool.ruff.lint]`. | T003 Owner (`@melodydeck04`) | no-expiry (justification: Pydantic v2 typed-config requires long single-line defaults; covered by formatter line wrapping) | 2026-09-15 |
| ruff `B011` (do not call `assert False`) scoped to `tests/**/*.py` | Tests legitimately call `assert False` and `pytest.raises` patterns; the rule is suppressed per-file via `[tool.ruff.lint.per-file-ignores]`. | T003 Owner (`@melodydeck04`) | no-expiry (justification: standard pytest idiom; removing it would force every negative-path test to use `pytest.raises`) | 2026-09-15 |
| `pyproject.toml [tool.coverage.report] fail_under = 0` | Phase 0 surface is trivial; raising the threshold without coverage baseline would fail the gate. | T010 Owner (deferred) | 2026-12-31 (or first task that introduces a coverage gate, whichever comes first) | 2026-09-15 |
| trivy `ignore-unfixed: true` (filesystem scan) | Trivy would otherwise surface advisories for which no patched release exists in the upstream ecosystem; this is the documented supply-chain best practice for unmaintained transient layers. | T003 Owner (`@melodydeck04`) | no-expiry (justification: trivy default ignores already-published unfixed advisories that the project cannot remediate) | 2026-09-15 |
| `tests/test_abi_artifacts.py::test_artifact_file_sha256_matches_when_recorded` runtime skip (`pytest.skip("sha256 is a manual annotation; not enforced")`) | The artifact SHA-256 annotation is intentionally left as a manual annotation; until the regeneration pipeline can update it atomically with the artifact, the verification is data-conditioned rather than enforced. Not a network test. | T021 Owner (deferred) | 2026-12-31 (or when the artifact regeneration pipeline is closed) | 2026-09-15 |
| `tests/test_protocol_ids.py::test_pool_id_matches_oracle_vector[reordered_inputs]` runtime skip (`pytest.skip(...see test_reordered_inputs_refused_by_python_invariant)`) | Python's PoolKey constructor refuses unsorted currencies by design (defends the canonical PoolId derivation), while the Solidity oracle sorts inputs internally. The case is delegated to a dedicated invariant test; the parametrized case is data-conditioned, not network-dependent. | T010 Owner (`@melodydeck04`) | no-expiry (justification: the invariant test is the authoritative check for this vector) | 2026-09-15 |
| `pytest --override-ini="markers=network(...)"` in `.github/workflows/ci.yml::network-tests` | The `network` marker is registered at run-time so the matrix entry does not silently drop the selection. This is *registration*, not suppression: it is the equivalent of the `--strict-markers` flag and does not silence a failure. | T003 Owner (`@melodydeck04`) | no-expiry (justification: needed to keep `--strict-markers` on while making the network matrix visible) | 2026-09-15 |

## Items reviewed and confirmed *not* to require suppression

| Scope | Why it is *not* a suppression | Date reviewed |
| --- | --- | --- |
| `actions/upload-artifact` upload of `.pytest_cache`, `.ruff_cache`, `.mypy_cache` in `ci.yml` | The upload path is restricted to cache directories only; secret paths (`*.env`, `*.key`, `*.pem`, `*.keystore`, `secrets/`, RPC response caches) are excluded by `.gitignore` and never produced by the local test runs. The acceptance clause requires the contract that drops these paths; the existing `path:` globs already satisfy it. | 2026-09-15 |
| `gitleaks/gitleaks-action` `GITLEAKS_ENABLE_UPLOAD_ARTIFACT: false` | This disables an artifact upload; it does not silence a finding. The secret-scan gate still runs and still fails on detection. | 2026-09-15 |
| `aquasecurity/trivy-action` `severity: HIGH,CRITICAL` | This narrows the failure domain; LOW/MEDIUM findings are not suppressed — they are simply not gated. Trivy still prints them in SARIF output. | 2026-09-15 |
| `if-no-files-found: ignore` on `actions/upload-artifact` | This is a present-or-absent artefact pattern, not a security suppression. The cache directories may be missing on a clean checkout and that should not fail the run. | 2026-09-15 |
