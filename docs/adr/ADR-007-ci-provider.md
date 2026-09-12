---
id: ADR-007
title: Continuous integration provider
status: proposed
date: 2026-09-12
owner: T000
supersedes: []
references: [R21, R22]
---

# ADR-007 — Continuous integration provider

## Context

Phase 0 T003 requires reproducible quality gates: tests, formatter,
linter, type checker, secret scan, dependency vulnerability scan, and
a lockfile integrity check. The provider must:

- pin every action and dependency by SHA, not by floating tag;
- run on every push and pull request without manual intervention;
- fail closed on secrets, vulnerable dependencies, and broken tests;
- keep artifacts (logs, reports, coverage) for a declared retention;
- be operable by the project owner without a dedicated SRE.

## Decision

Adopt **GitHub Actions** as the CI provider for this repository.

Rationale:

- the repository already lives on GitHub, so there is no extra trust
  boundary or account to manage;
- GitHub Actions supports pinning actions by full commit SHA with
  comment-based version notes;
- it integrates natively with branch protection, required status
  checks, and secret scanning (which is also enabled at the repo
  level).

Pinning rules:

- every `uses: owner/action@<full-sha>` reference must include a
  comment with the resolved semver tag for human readability;
- floating tags (`@v1`, `@main`) are forbidden;
- lockfiles (`uv.lock` / `poetry.lock`) are committed and verified in
  CI via the package manager's own verification command;
- CI uploads no `.env` values, raw RPC responses, or coverage HTML
  containing credentialed URLs.

Required checks (initial set, expanded by T003):

- `pytest` on Python 3.12 with the locked environment;
- `ruff format --check` and `ruff check`;
- the strict type checker selected in T001;
- `gitleaks` (or equivalent) secret scan;
- `osv-scanner` (or equivalent) dependency vulnerability scan;
- an `import-linter` / `depend` check enforcing ADR-006.

Suppression is allowed only with `reason`, `owner`, and `expiry` fields
in a project-controlled allowlist file.

## Alternatives considered

- **Travis CI.** Mature but increasingly expensive for private repos
  with no advantage for this project.
- **CircleCI.** Good DX but adds another vendor and billing surface.
- **Self-hosted Jenkins / Drone.** Maximum control but requires
  dedicated operational attention, which is out of scope for this
  project.

## Consequences

Positive:

- tight integration with branch protection and secret scanning on
  the same platform that hosts the code;
- SHA-pinned actions give reproducible runs even if upstream tags are
  force-pushed;
- small workflow files are reviewable in pull requests alongside the
  code they test.

Negative / risks:

- GitHub Actions is itself a supply-chain dependency; SHA pinning
  mitigates but does not eliminate the risk, and the project must
  re-evaluate pinned actions on a schedule;
- secret scanning and dependency review depend on GitHub-specific
  features; if the project moves to another host, this ADR must be
  revisited together with the migration.

## Migration trigger

Re-evaluate this ADR if any of the following occur:

- a critical CVE affects GitHub Actions and the workaround is to
  move CI off-platform;
- the project requires a CI feature that GitHub Actions does not
  provide (e.g. self-hosted runners with custom hardware);
- the project moves its primary remote off GitHub.

## Owner

T000 (initial). Hand off to whoever owns CI configuration in T003.
