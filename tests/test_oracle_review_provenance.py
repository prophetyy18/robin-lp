"""Review-evidence verifier for the protocol vector fixtures (T013).

Every edge class in the pinned JSON fixtures must carry at least one
reviewer entry in ``_meta.reviews`` whose identity is verifiable by
``git log`` (signature_method = ``git-author-commit``) or by GPG
(``gpg --verify``, currently out of scope but the field is reserved
for forward compatibility).

Contract rules (T013 acceptance):

1. Every review entry must carry: ``edge_class`` (str), ``vector_names``
   (non-empty list[str]), ``reviewer`` (str, must contain ``@``),
   ``reviewed_at`` (ISO-8601 UTC str), ``content_sha256`` (64-char
   lowercase hex), ``signature_method`` (one of ``git-author-commit``
   or ``gpg``), and ``review_ref`` (str).
2. For ``signature_method: git-author-commit``, the test runs
   ``git rev-parse --verify <review_ref>`` against the candidate
   Developer worktree, then ``git log -1 --format='%ae%n%B' <ref>``
   to fetch the author email and commit body. It asserts:

   - The author email equals the ``reviewer`` field.
   - The commit body carries trailers
     ``Reviewed-Edge-Class: <edge_class>``,
     ``Reviewed-Vectors: <comma-separated vector_names>``, and
     ``Reviewed-SHA256: <content_sha256>`` matching the JSON record.
3. For ``signature_method: gpg``, the test emits ``pytest.skip``
   with a message explaining that gpg verification is reserved and
   out of scope for this attempt. The contract permits this; the
   test infrastructure does not have to enforce gpg today.
4. The reviewer email must NOT equal the candidate Developer email
   (resolved via ``git log -1 --format=%ae HEAD``).
5. Every edge class referenced in the fixture must have at least one
   review entry.

The fixture's ``_meta`` may carry a ``review_strategy`` field
documenting which path (A: git-author-commit from a historical
reviewer, or B: reserved gpg) the fixture author chose.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
POOL_ID_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "protocol" / "pool_id_vectors.json"
MATH_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "protocol" / "math_vectors.json"

ALLOWED_SIGNATURE_METHODS = frozenset({"git-author-commit", "gpg"})

# Edge classes that MUST have at least one review entry.
POOL_ID_EDGE_CLASSES = (
    "v1_static_3000_60",
    "native_currency0",
    "dynamic_fee_with_hook",
    "max_static_fee",
    "max_tick_spacing",
    "hook_with_delta_action",
    "reordered_inputs",
)
MATH_EDGE_CLASSES = (
    "tick_to_sqrt_price",
    "sqrt_price_to_tick",
    "amount_deltas",
    "liquidity_for_amounts",
)


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _candidate_developer_email() -> str:
    """Return the author email of HEAD in the candidate Developer worktree."""
    return _git("log", "-1", "--format=%ae", "HEAD")


def _load_fixture(path: Path) -> dict[str, Any]:
    if not path.is_file():
        pytest.fail(f"fixture not found at {path}")
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Field-level validation
# ---------------------------------------------------------------------------


def _validate_review_entry(entry: Any, index: int) -> None:
    assert isinstance(entry, dict), f"reviews[{index}] must be a dict"
    for required in (
        "edge_class",
        "vector_names",
        "reviewer",
        "reviewed_at",
        "content_sha256",
        "signature_method",
        "review_ref",
    ):
        assert required in entry, f"reviews[{index}] missing field {required!r}"

    assert isinstance(entry["edge_class"], str) and entry["edge_class"], (
        f"reviews[{index}].edge_class must be a non-empty string"
    )
    vector_names = entry["vector_names"]
    assert isinstance(vector_names, list) and vector_names, (
        f"reviews[{index}].vector_names must be a non-empty list"
    )
    for vn in vector_names:
        assert isinstance(vn, str) and vn, (
            f"reviews[{index}].vector_names entries must be non-empty strings"
        )

    assert isinstance(entry["reviewer"], str) and "@" in entry["reviewer"], (
        f"reviews[{index}].reviewer must contain '@': got {entry['reviewer']!r}"
    )
    assert isinstance(entry["reviewed_at"], str), f"reviews[{index}].reviewed_at must be a string"
    # Loose ISO-8601 check: YYYY-MM-DD prefix and time component.
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$",
        entry["reviewed_at"],
    ), f"reviews[{index}].reviewed_at must be ISO-8601 UTC"

    sha = entry["content_sha256"]
    assert isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha), (
        f"reviews[{index}].content_sha256 must be 64 lowercase hex chars"
    )

    sig = entry["signature_method"]
    assert sig in ALLOWED_SIGNATURE_METHODS, (
        f"reviews[{index}].signature_method must be one of "
        f"{sorted(ALLOWED_SIGNATURE_METHODS)}, got {sig!r}"
    )

    assert isinstance(entry["review_ref"], str) and entry["review_ref"], (
        f"reviews[{index}].review_ref must be a non-empty string"
    )


def _verify_git_author_commit(entry: dict[str, Any], developer_email: str) -> None:
    ref = entry["review_ref"]
    # Resolve the ref to a full SHA in the candidate Developer worktree.
    try:
        sha = _git("rev-parse", "--verify", ref)
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"review entry references unresolvable git ref {ref!r}: {exc.stderr.strip()}")
    assert re.fullmatch(r"[0-9a-f]{40}", sha), (
        f"review_ref {ref!r} did not resolve to a 40-char SHA"
    )

    log_output = _git("log", "-1", "--format=%ae%n%B", sha)
    author_email, _, body = log_output.partition("\n")
    body = body.lstrip("\n")
    assert author_email == entry["reviewer"], (
        f"git-author-commit reviewer mismatch: commit {sha} has "
        f"author email {author_email!r} but the review entry claims "
        f"reviewer {entry['reviewer']!r}"
    )

    # Anti-self-attestation: the candidate Developer cannot self-review.
    assert author_email != developer_email, (
        f"review entry author email {author_email!r} equals the "
        f"candidate Developer email; T013 forbids self-attestation."
    )

    # Trailer checks.
    vector_names_csv = ",".join(entry["vector_names"])
    expected_trailers = {
        "Reviewed-Edge-Class": entry["edge_class"],
        "Reviewed-Vectors": vector_names_csv,
        "Reviewed-SHA256": entry["content_sha256"],
    }
    for trailer, expected in expected_trailers.items():
        pattern = rf"(?m)^{re.escape(trailer)}:\s*{re.escape(expected)}\s*$"
        assert re.search(pattern, body), (
            f"review entry for {entry['edge_class']!r} is missing "
            f"or has wrong trailer {trailer}: expected {expected!r}"
            f"\ncommit body:\n{body}"
        )


# ---------------------------------------------------------------------------
# Per-fixture checks
# ---------------------------------------------------------------------------


def _collect_reviews(fixture_path: Path, edge_classes: Iterable[str]) -> list[dict[str, Any]]:
    fixture = _load_fixture(fixture_path)
    meta = fixture.get("_meta", {})
    assert isinstance(meta, dict), f"{fixture_path} _meta must be a dict"
    reviews = meta.get("reviews")
    assert isinstance(reviews, list) and reviews, (
        f"{fixture_path} _meta.reviews must be a non-empty list"
    )
    for idx, entry in enumerate(reviews):
        _validate_review_entry(entry, idx)
    # Every edge class must have at least one review entry.
    reviewed_classes = {entry["edge_class"] for entry in reviews}
    missing = set(edge_classes) - reviewed_classes
    assert not missing, f"{fixture_path}: missing reviews for edge classes: {sorted(missing)}"
    return reviews


def test_pool_id_fixture_reviews_are_well_formed() -> None:
    _collect_reviews(POOL_ID_FIXTURE, POOL_ID_EDGE_CLASSES)


def test_math_fixture_reviews_are_well_formed() -> None:
    _collect_reviews(MATH_FIXTURE, MATH_EDGE_CLASSES)


def test_pool_id_fixture_reviews_resolve_to_real_reviewers() -> None:
    """Verify every git-author-commit review entry against git log."""
    developer_email = _candidate_developer_email()
    for entry in _collect_reviews(POOL_ID_FIXTURE, POOL_ID_EDGE_CLASSES):
        if entry["signature_method"] == "git-author-commit":
            _verify_git_author_commit(entry, developer_email)
        else:
            pytest.skip(
                "gpg verification is out of scope for this attempt; the "
                "signature_method field is reserved"
            )


def test_math_fixture_reviews_resolve_to_real_reviewers() -> None:
    """Verify every git-author-commit review entry against git log."""
    developer_email = _candidate_developer_email()
    for entry in _collect_reviews(MATH_FIXTURE, MATH_EDGE_CLASSES):
        if entry["signature_method"] == "git-author-commit":
            _verify_git_author_commit(entry, developer_email)
        else:
            pytest.skip(
                "gpg verification is out of scope for this attempt; the "
                "signature_method field is reserved"
            )


def test_allowed_signers_file_exists_for_gpg_allowlist() -> None:
    """The gpg verification path is reserved, but the allowlist file
    must exist at the documented path so that a future attempt can
    populate it without re-negotiating the contract."""
    allowlist = REPO_ROOT / "tools" / "reviewers" / "allowed_signers"
    assert allowlist.is_file(), f"missing allowlist at {allowlist}"
