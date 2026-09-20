"""Immutable candidate-lock record (T066).

The candidate-lock record is the *sealing* artifact T066 binds the
test-fold gate to. The harness writes the lock BEFORE the test fold
is read; the lock carries every frozen field that defines a
candidate version, and its content hash binds those fields. A
change to any frozen field after the lock is recorded creates a new
candidate version (a new lock) rather than an edit to the existing
one — the lock is append-only.

The frozen fields (the "protocol" a candidate will be judged under):

- ``parameter_set_version`` — the deterministic version string of
  the parameter set the candidate was tuned on. The harness uses
  ``compute_parameter_set_version`` (defined in
  :mod:`robinhood_lp.experiments.search`) so a parameter change
  produces a new version rather than an edit.
- ``code_revision`` — the code revision the candidate depends on
  (git SHA, or :data:`UNKNOWN_CODE_REVISION`).
- ``manifest_hash`` — the SHA-256 hex digest of the experiment
  manifest the candidate was evaluated against; the lock binds the
  candidate to the *exact* manifest content, not just the dataset
  version.
- ``seed`` — the seed the candidate was tuned on.
- ``split_boundaries`` — the declared split-boundary records the
  candidate was tuned under. Reusing T064's
  :class:`robinhood_lp.robustness.splits.SplitBoundary`, the lock
  carries one entry per fold edge so a change to any boundary
  yields a new lock.
- ``cost_assumptions`` — the closed-vocabulary cost assumptions
  (gas / slippage / fee). The lock carries them as a frozen tuple
  of strings.
- ``elimination_rules`` — the elimination rules the candidate was
  judged under (e.g. the DS-035 outcome catalogue, the
  ``EXPERIMENTAL_NOT_LIVE_APPROVED`` carry-forward flag, the
  "primary benchmark = USDG cash" rule). The harness refuses to
  evaluate any candidate whose lock does not carry the
  ``PRIMARY_BENCHMARK_USDG_CASH`` rule and the
  ``EXPERIMENTAL_NOT_LIVE_APPROVED`` carry-forward rule.

Recorded fields (the *evidence* the lock was written):

- ``content_hash`` — the SHA-256 hex digest of the canonical JSON
  serialisation of every frozen field.
- ``actor`` — the actor that wrote the lock (a non-empty string).
- ``lock_time_unix_seconds`` — the integer Unix-seconds time the
  lock was written. The harness uses a caller-supplied time
  (no wall-clock reads inside the lock module) so the same lock
  is byte-identical across processes.

The lock is persisted as JSON. The on-disk format is the canonical
JSON serialisation of the dataclass's :meth:`to_dict` output;
:meth:`CandidateLock.from_dict` reads it back, recomputes the
content hash, and rejects a tampered file.

Design constraints (binding):

- **Append-only.** The dataclass is frozen. There is no
  ``update`` / ``amend`` path; a parameter change writes a new
  lock to a new location.
- **Integer / UTF-8.** ``float`` does not appear on the lock path.
- **No wall-clock reads.** The lock module never imports
  ``time.time``; the caller supplies the lock time so reruns are
  byte-equivalent.
- **Layer purity.** This module imports the standard library and
  the in-package robustness / search modules only. It does not
  import the backtest engine, the manifest layer, RPC, storage,
  signing, execution, or presentation code.

References:

- T066 — Build versioned threshold experiments and provisional
  candidate review.
- `docs/spec/strategy/STRATEGY_ECONOMICS.md` §6 (threshold /
  experiment discipline).
- `docs/spec/research/DATASET_AND_EVALUATION.md` DS-031
  (candidate locking before test fold).
- T064 — split boundaries the lock freezes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from robinhood_lp.robustness.splits import (
    VALID_AXIS_KINDS,
    VALID_RECORDED_SOURCES,
    InvalidSplitBoundaryError,
    SplitBoundary,
    make_boundary_id,
)

#: Module version. Bumping it is a breaking change for downstream
#: consumers (the harness, the audit chain).
CANDIDATE_LOCK_VERSION: Final[str] = "t066.candidate_lock.v1"

#: Sentinel cost-assumption strings the lock records. The harness
#: requires every candidate lock to carry at least one entry from
#: this vocabulary; the values mirror the T063 manifest vocabulary.
VALID_COST_ASSUMPTIONS: Final[frozenset[str]] = frozenset(
    {"FLAT_GAS", "DYNAMIC_GAS", "STATIC_SLIPPAGE", "PROBABILISTIC_SLIPPAGE"}
)

#: Sentinel elimination-rule strings the lock records. The harness
#: rejects a lock that omits the binding rules the contract pins
#: to the candidate: the primary benchmark must be USDG cash, the
#: "do not retune on test" rule must be present, the no-trade
#: carry-forward must be present, and the
#: ``EXPERIMENTAL_NOT_LIVE_APPROVED`` carry-forward must be present.
REQUIRED_ELIMINATION_RULES: Final[frozenset[str]] = frozenset(
    {
        "PRIMARY_BENCHMARK_USDG_CASH",
        "NO_RETUNE_ON_TEST",
        "PRESERVE_NO_TRADE_PERIODS",
        "PRESERVE_ALL_LOSING_REJECTED_RUNS",
        "EXPERIMENTAL_NOT_LIVE_APPROVED",
    }
)

#: Sentinel code-revision string when no git revision is available.
UNKNOWN_CODE_REVISION: Final[str] = "UNKNOWN"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CandidateLockError(ValueError):
    """Base class for candidate-lock construction / validation failures."""


class InvalidCandidateLockError(CandidateLockError):
    """A candidate-lock field violates its invariant."""


class CandidateLockNotFoundError(CandidateLockError):
    """A requested candidate lock is not present at the supplied path."""


class CandidateLockHashMismatchError(CandidateLockError):
    """The candidate lock's content hash disagrees with the recomputed digest."""


class CandidateLockMissingRequiredRuleError(CandidateLockError):
    """A candidate lock omits a binding elimination rule."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_str(value: object, *, field: str) -> str:
    if not isinstance(value, str):
        raise InvalidCandidateLockError(f"{field}: must be str, got {type(value).__name__}")
    return value


def _require_non_empty_str(value: object, *, field: str) -> str:
    s = _require_str(value, field=field)
    if not s:
        raise InvalidCandidateLockError(f"{field}: must be non-empty str")
    return s


def _require_non_negative_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCandidateLockError(f"{field}: must be int, got {type(value).__name__}")
    if value < 0:
        raise InvalidCandidateLockError(f"{field}: must be non-negative, got {value}")
    return int(value)


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCandidateLockError(f"{field}: must be int, got {type(value).__name__}")
    return int(value)


# ---------------------------------------------------------------------------
# CandidateLock
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateLock:
    """The immutable candidate-lock record T066 binds the test-fold gate to.

    The lock freezes the protocol a candidate will be judged under:
    parameter-set version, code revision, manifest hash, seed,
    split boundaries, cost assumptions and elimination rules. The
    content hash binds those fields; ``actor`` and
    ``lock_time_unix_seconds`` record *who* and *when* wrote the
    lock so an audit reviewer can trace provenance.

    Field units:

    - ``version`` — lock schema version (string).
    - ``candidate_id`` — non-empty string; the candidate version's
      primary key. The harness derives it from the parameter-set
      version (so a parameter change is a new candidate) and the
      actor / lock time supply a *secondary* check that no two
      locks for the same candidate overwrite each other.
    - ``parameter_set_version`` — non-empty hex string; the
      deterministic version of the parameter set the candidate was
      tuned on.
    - ``code_revision`` — git SHA, or :data:`UNKNOWN_CODE_REVISION`.
    - ``manifest_hash`` — non-empty hex string; the SHA-256 digest
      of the experiment manifest.
    - ``seed`` — non-negative integer.
    - ``split_boundaries`` — non-empty tuple of
      :class:`SplitBoundary`; the harness requires at least one
      boundary so a "no splits declared" lock is rejected.
    - ``cost_assumptions`` — non-empty tuple of strings from
      :data:`VALID_COST_ASSUMPTIONS`.
    - ``elimination_rules`` — non-empty tuple of strings that must
      include every name in :data:`REQUIRED_ELIMINATION_RULES`.
    - ``content_hash`` — the SHA-256 hex digest of the canonical
      serialisation of every frozen field above; computed by the
      factory function and verified on load.
    - ``actor`` — non-empty string; the actor that wrote the lock.
    - ``lock_time_unix_seconds`` — non-negative integer Unix
      seconds the lock was written.
    """

    version: str
    candidate_id: str
    parameter_set_version: str
    code_revision: str
    manifest_hash: str
    seed: int
    split_boundaries: tuple[SplitBoundary, ...]
    cost_assumptions: tuple[str, ...]
    elimination_rules: tuple[str, ...]
    content_hash: str
    actor: str
    lock_time_unix_seconds: int

    def __post_init__(self) -> None:
        if self.version != CANDIDATE_LOCK_VERSION:
            raise InvalidCandidateLockError(
                f"CandidateLock.version: must be {CANDIDATE_LOCK_VERSION!r}, got {self.version!r}"
            )
        _require_non_empty_str(self.candidate_id, field="CandidateLock.candidate_id")
        _require_non_empty_str(
            self.parameter_set_version, field="CandidateLock.parameter_set_version"
        )
        if not (
            self.code_revision == UNKNOWN_CODE_REVISION
            or (
                len(self.code_revision) >= 7
                and all(c in "0123456789abcdef" for c in self.code_revision)
            )
        ):
            # Accept a hex SHA or the sentinel; reject anything else.
            raise InvalidCandidateLockError(
                "CandidateLock.code_revision: must be a hex SHA or "
                f"{UNKNOWN_CODE_REVISION!r}, got {self.code_revision!r}"
            )
        _require_non_empty_str(self.manifest_hash, field="CandidateLock.manifest_hash")
        _require_non_negative_int(self.seed, field="CandidateLock.seed")
        if not self.split_boundaries:
            raise InvalidCandidateLockError(
                "CandidateLock.split_boundaries: must be non-empty tuple"
            )
        for i, boundary in enumerate(self.split_boundaries):
            if not isinstance(boundary, SplitBoundary):
                raise InvalidCandidateLockError(
                    f"CandidateLock.split_boundaries[{i}]: must be SplitBoundary, "
                    f"got {type(boundary).__name__}"
                )
        if not self.cost_assumptions:
            raise InvalidCandidateLockError(
                "CandidateLock.cost_assumptions: must be non-empty tuple"
            )
        unknown_costs = [c for c in self.cost_assumptions if c not in VALID_COST_ASSUMPTIONS]
        if unknown_costs:
            raise InvalidCandidateLockError(
                f"CandidateLock.cost_assumptions: unknown value(s) "
                f"{unknown_costs!r}; valid={sorted(VALID_COST_ASSUMPTIONS)}"
            )
        if not self.elimination_rules:
            raise InvalidCandidateLockError(
                "CandidateLock.elimination_rules: must be non-empty tuple"
            )
        missing_rules = sorted(set(REQUIRED_ELIMINATION_RULES) - set(self.elimination_rules))
        if missing_rules:
            raise CandidateLockMissingRequiredRuleError(
                f"CandidateLock.elimination_rules: missing required rule(s) "
                f"{missing_rules!r}; required={sorted(REQUIRED_ELIMINATION_RULES)}"
            )
        _require_non_empty_str(self.content_hash, field="CandidateLock.content_hash")
        _require_non_empty_str(self.actor, field="CandidateLock.actor")
        _require_non_negative_int(
            self.lock_time_unix_seconds, field="CandidateLock.lock_time_unix_seconds"
        )

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        """Return the JSON-friendly serialisation of this lock.

        The serialisation sorts every collection so two equivalent
        locks produce byte-identical JSON. ``content_hash`` is
        included so a downstream reader can verify the file
        without re-computing the digest.
        """
        return {
            "version": self.version,
            "candidate_id": self.candidate_id,
            "parameter_set_version": self.parameter_set_version,
            "code_revision": self.code_revision,
            "manifest_hash": self.manifest_hash,
            "seed": self.seed,
            "split_boundaries": [b.to_dict() for b in self.split_boundaries],
            "cost_assumptions": list(self.cost_assumptions),
            "elimination_rules": list(self.elimination_rules),
            "content_hash": self.content_hash,
            "actor": self.actor,
            "lock_time_unix_seconds": self.lock_time_unix_seconds,
        }

    def to_canonical_json(self) -> str:
        """Return the canonical JSON serialisation of the frozen fields.

        The function serialises the frozen fields (everything
        except ``content_hash`` itself) so the same hash function
        computes the lock digest on both write and read.
        """
        payload = self._frozen_payload()
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _frozen_payload(self) -> dict[str, object]:
        """Return the payload the content hash is computed over.

        The payload is every frozen field — the values that
        define the candidate version. ``content_hash`` itself,
        ``actor`` and ``lock_time_unix_seconds`` are excluded so a
        change to provenance does not change the candidate
        version; ``content_hash`` is bound to the *frozen* fields
        only.
        """
        return {
            "version": self.version,
            "candidate_id": self.candidate_id,
            "parameter_set_version": self.parameter_set_version,
            "code_revision": self.code_revision,
            "manifest_hash": self.manifest_hash,
            "seed": self.seed,
            "split_boundaries": [b.to_dict() for b in self.split_boundaries],
            "cost_assumptions": list(self.cost_assumptions),
            "elimination_rules": list(self.elimination_rules),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> CandidateLock:
        """Reconstruct a :class:`CandidateLock` from a JSON-friendly ``dict``.

        The function recomputes the content hash from the
        declared frozen fields and rejects a payload whose
        ``content_hash`` slot disagrees with the recomputed
        digest — the tamper-detection gate is the first thing a
        re-runnable reader checks.
        """
        if not isinstance(payload, Mapping):
            raise InvalidCandidateLockError(
                f"CandidateLock.from_dict: payload must be Mapping, got {type(payload).__name__}"
            )
        try:
            candidate_id = _require_non_empty_str(
                payload["candidate_id"], field="CandidateLock.from_dict.candidate_id"
            )
            parameter_set_version = _require_non_empty_str(
                payload["parameter_set_version"],
                field="CandidateLock.from_dict.parameter_set_version",
            )
            code_revision = _require_str(
                payload["code_revision"],
                field="CandidateLock.from_dict.code_revision",
            )
            manifest_hash = _require_non_empty_str(
                payload["manifest_hash"],
                field="CandidateLock.from_dict.manifest_hash",
            )
            seed = _require_non_negative_int(payload["seed"], field="CandidateLock.from_dict.seed")
            actor = _require_non_empty_str(payload["actor"], field="CandidateLock.from_dict.actor")
            lock_time = _require_non_negative_int(
                payload["lock_time_unix_seconds"],
                field="CandidateLock.from_dict.lock_time_unix_seconds",
            )
            content_hash = _require_non_empty_str(
                payload["content_hash"],
                field="CandidateLock.from_dict.content_hash",
            )
            cost_assumptions_raw = payload["cost_assumptions"]
            elimination_rules_raw = payload["elimination_rules"]
            split_boundaries_raw = payload["split_boundaries"]
        except KeyError as exc:
            raise InvalidCandidateLockError(
                f"CandidateLock.from_dict: missing key {exc.args[0]!r}"
            ) from exc
        if not isinstance(cost_assumptions_raw, (list, tuple)):
            raise InvalidCandidateLockError(
                "CandidateLock.from_dict.cost_assumptions: must be list/tuple"
            )
        if not isinstance(elimination_rules_raw, (list, tuple)):
            raise InvalidCandidateLockError(
                "CandidateLock.from_dict.elimination_rules: must be list/tuple"
            )
        if not isinstance(split_boundaries_raw, (list, tuple)):
            raise InvalidCandidateLockError(
                "CandidateLock.from_dict.split_boundaries: must be list/tuple"
            )
        cost_assumptions = tuple(str(c) for c in cost_assumptions_raw)
        elimination_rules = tuple(str(r) for r in elimination_rules_raw)
        split_boundaries = tuple(_coerce_boundary(b) for b in split_boundaries_raw)
        # Reconstruct with placeholder content_hash, then verify.
        lock = cls(
            version=CANDIDATE_LOCK_VERSION,
            candidate_id=candidate_id,
            parameter_set_version=parameter_set_version,
            code_revision=code_revision,
            manifest_hash=manifest_hash,
            seed=seed,
            split_boundaries=split_boundaries,
            cost_assumptions=cost_assumptions,
            elimination_rules=elimination_rules,
            content_hash="0x" + "00" * 32,
            actor=actor,
            lock_time_unix_seconds=lock_time,
        )
        expected_hash = compute_candidate_lock_content_hash(lock)
        if expected_hash != content_hash:
            raise CandidateLockHashMismatchError(
                f"CandidateLock.from_dict: declared content_hash={content_hash} "
                f"disagrees with recomputed digest={expected_hash}; the file "
                f"was tampered with or written by a different candidate "
                f"version"
            )
        # Replace the placeholder with the verified hash so the
        # reconstructed lock is byte-equivalent to the on-disk
        # record. ``object.__setattr__`` is required because the
        # dataclass is frozen.
        object.__setattr__(lock, "content_hash", content_hash)
        return lock


# ---------------------------------------------------------------------------
# Content hash
# ---------------------------------------------------------------------------


def _coerce_boundary(boundary: object) -> SplitBoundary:
    """Return a :class:`SplitBoundary` constructed from ``boundary``.

    Accepts either an existing :class:`SplitBoundary` (returned
    unchanged) or a mapping with the boundary's documented keys.
    The mapping form is what :meth:`CandidateLock.from_dict`
    consumes from the on-disk JSON; the constructor's
    ``boundary_id`` slot is recomputed so a tampered file cannot
    smuggle a stale id past the load gate.
    """
    if isinstance(boundary, SplitBoundary):
        return boundary
    if not isinstance(boundary, Mapping):
        raise InvalidCandidateLockError(
            f"_coerce_boundary: must be SplitBoundary or Mapping, got {type(boundary).__name__}"
        )
    try:
        chain_id = _require_int(boundary["chain_id"], field="_coerce_boundary.chain_id")
        if chain_id <= 0:
            raise InvalidCandidateLockError(
                f"_coerce_boundary.chain_id: must be positive, got {chain_id}"
            )
        pool_key_id = _require_non_empty_str(
            boundary["pool_key_id"], field="_coerce_boundary.pool_key_id"
        )
        axis_kind = _require_str(boundary["axis_kind"], field="_coerce_boundary.axis_kind")
        if axis_kind not in VALID_AXIS_KINDS:
            raise InvalidCandidateLockError(
                f"_coerce_boundary.axis_kind: must be one of "
                f"{sorted(VALID_AXIS_KINDS)}, got {axis_kind!r}"
            )
        fold_index = _require_non_negative_int(
            boundary["fold_index"], field="_coerce_boundary.fold_index"
        )
        fold_role = _require_non_empty_str(
            boundary["fold_role"], field="_coerce_boundary.fold_role"
        )
        segment_label = _require_non_empty_str(
            boundary["segment_label"], field="_coerce_boundary.segment_label"
        )
        anchor_value = _require_non_negative_int(
            boundary["anchor_value"], field="_coerce_boundary.anchor_value"
        )
        anchor_unit = _require_str(boundary["anchor_unit"], field="_coerce_boundary.anchor_unit")
        recorded_at = _require_non_negative_int(
            boundary["recorded_at_unix_seconds"],
            field="_coerce_boundary.recorded_at_unix_seconds",
        )
        recorded_source = _require_str(
            boundary["recorded_source"], field="_coerce_boundary.recorded_source"
        )
        if recorded_source not in VALID_RECORDED_SOURCES:
            raise InvalidCandidateLockError(
                f"_coerce_boundary.recorded_source: must be one of "
                f"{sorted(VALID_RECORDED_SOURCES)}, got {recorded_source!r}"
            )
    except KeyError as exc:
        raise InvalidCandidateLockError(f"_coerce_boundary: missing key {exc.args[0]!r}") from exc
    # Recompute the deterministic boundary id so a tampered file
    # cannot smuggle a stale id past the load gate.
    boundary_id = make_boundary_id(
        chain_id=chain_id,
        pool_key_id=pool_key_id,
        axis_kind=axis_kind,
        fold_index=fold_index,
        fold_role=fold_role,
        segment_label=segment_label,
        anchor_value=anchor_value,
        anchor_unit=anchor_unit,
        recorded_source=recorded_source,
    )
    # The SplitBoundary constructor itself enforces the cross-field
    # invariants (anchor >= 0, axis in vocabulary, ...). A failure
    # here surfaces as :class:`InvalidSplitBoundaryError`, which
    # is a subclass of ``ValueError``; the load path treats it as a
    # lock tamper.
    try:
        return SplitBoundary(
            chain_id=chain_id,
            pool_key_id=pool_key_id,
            axis_kind=axis_kind,
            fold_index=fold_index,
            fold_role=fold_role,
            segment_label=segment_label,
            anchor_value=anchor_value,
            anchor_unit=anchor_unit,
            recorded_at_unix_seconds=recorded_at,
            recorded_source=recorded_source,
            boundary_id=boundary_id,
        )
    except InvalidSplitBoundaryError as exc:
        raise InvalidCandidateLockError(
            f"_coerce_boundary: SplitBoundary constructor rejected the mapping: {exc}"
        ) from exc


def compute_candidate_lock_content_hash(lock: CandidateLock) -> str:
    """Return the SHA-256 hex digest of the canonical lock payload.

    The digest binds the frozen fields (``_frozen_payload``) so a
    change to any of them yields a new hash; the digest is the
    tamper-detection gate on disk. ``content_hash``,
    ``lock_time_unix_seconds`` and ``actor`` are deliberately
    excluded so provenance metadata does not change the candidate
    version.
    """
    payload = lock._frozen_payload()
    content = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def build_candidate_lock(
    *,
    candidate_id: str,
    parameter_set_version: str,
    code_revision: str,
    manifest_hash: str,
    seed: int,
    split_boundaries: Iterable[object],
    cost_assumptions: Sequence[str],
    elimination_rules: Sequence[str],
    actor: str,
    lock_time_unix_seconds: int,
) -> CandidateLock:
    """Build a :class:`CandidateLock` and compute its content hash.

    The function is the canonical builder: it normalises
    ``split_boundaries`` (accepting either :class:`SplitBoundary`
    or any mapping form :func:`boundary_like_to_boundary`
    understands), sorts the rule / cost tuples, computes the
    content hash, and binds it onto the lock.

    The harness writes the lock returned by this function to
    disk before it reads the test fold; the lock's
    ``content_hash`` is the tamper-detection gate.
    """
    normalised_boundaries = tuple(_coerce_boundary(b) for b in split_boundaries)
    sorted_costs = tuple(sorted(set(cost_assumptions)))
    sorted_rules = tuple(sorted(set(elimination_rules)))
    lock = CandidateLock(
        version=CANDIDATE_LOCK_VERSION,
        candidate_id=candidate_id,
        parameter_set_version=parameter_set_version,
        code_revision=code_revision,
        manifest_hash=manifest_hash,
        seed=seed,
        split_boundaries=normalised_boundaries,
        cost_assumptions=sorted_costs,
        elimination_rules=sorted_rules,
        content_hash="0x" + "00" * 32,
        actor=actor,
        lock_time_unix_seconds=lock_time_unix_seconds,
    )
    digest = compute_candidate_lock_content_hash(lock)
    object.__setattr__(lock, "content_hash", digest)
    return lock


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def write_candidate_lock(lock: CandidateLock, target_path: object) -> None:
    """Write ``lock`` to ``target_path`` as canonical JSON.

    The caller supplies the path object (typically a
    ``pathlib.Path``); the function opens the file, writes the
    canonical JSON, and closes. The on-disk format is
    :meth:`CandidateLock.to_dict` sorted by key with no extra
    whitespace.
    """
    import os
    from pathlib import Path

    path = Path(target_path)  # type: ignore[arg-type]
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(
        lock.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    fd = os.open(
        str(path),
        flags=os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        mode=0o644,
    )
    try:
        os.write(fd, content.encode("utf-8"))
    finally:
        os.close(fd)


def load_candidate_lock(source_path: object) -> CandidateLock:
    """Read and verify a :class:`CandidateLock` from ``source_path``.

    The function rejects a missing path with
    :class:`CandidateLockNotFoundError` and a tampered file with
    :class:`CandidateLockHashMismatchError`. The hash check is
    the gate the harness relies on: the harness calls this
    function before it reads the test fold, and any disagreement
    fails the harness closed.
    """
    from pathlib import Path

    path = Path(source_path)  # type: ignore[arg-type]
    if not path.exists():
        raise CandidateLockNotFoundError(
            f"load_candidate_lock: no lock at {path}; the harness refuses "
            f"to read the test fold until a matching candidate lock is "
            f"written for this version"
        )
    raw = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidCandidateLockError(
            f"load_candidate_lock: {path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidCandidateLockError(f"load_candidate_lock: {path} root must be a JSON object")
    return CandidateLock.from_dict(payload)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "CANDIDATE_LOCK_VERSION",
    "REQUIRED_ELIMINATION_RULES",
    "UNKNOWN_CODE_REVISION",
    "VALID_COST_ASSUMPTIONS",
    "CandidateLock",
    "CandidateLockError",
    "CandidateLockHashMismatchError",
    "CandidateLockMissingRequiredRuleError",
    "CandidateLockNotFoundError",
    "InvalidCandidateLockError",
    "build_candidate_lock",
    "compute_candidate_lock_content_hash",
    "load_candidate_lock",
    "write_candidate_lock",
]
