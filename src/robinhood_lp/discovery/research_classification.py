"""Research-universe pool classification (T027).

T023 introduced the reason-coded classifier that assigns one of the
five ``RunMode`` support levels to every pool in the registry. T027
extends that classifier so it can also classify pools that belong to
the research universe, not just the single active execution pool.

The classifier is keyed on ``(chain_id, PoolKey)`` alone. It does not
consult any target-token approval state, does not consult a token
symbol, and does not consult the active-pool collection. The output
is a :class:`ResearchClassificationDecision` carrying the same
evidence surface as :class:`EligibilityDecision` plus an explicit,
machine-readable statement that the classification is not a token
or pool approval and grants no execution authority (ADR-014).

T027 acceptance (todo/phases/P02-chain-access-and-discovery/T027.md):

- every registry row — execution candidate and research member alike
  — carries a level plus its evidence;
- unknown hooks and return-delta flags default to ``ingestion``;
- a behaviour / code-hash change demotes the pool;
- classification decisions are deterministic and audited;
- a research member below ``backtest`` is refused for replay,
  backtest and model research with its named reason
  (``ResearchSupportGateError``);
- a member with unverifiable hook semantics cannot be promoted
  beyond ``ingestion``;
- no classification outcome produced for a research member carries,
  or can be read as carrying, execution authority or token approval.

T027 must-not:

- infer semantics from hook address flags alone;
- whitelist by name;
- fall back to plain-pool behaviour;
- let a research classification stand in for a token or pool
  approval;
- classify by token symbol or by the target-token context;
- promote a research member above ``ingestion`` without the evidence
  the execution path would require.

Boundary cases the classifier covers:

- a pool whose metadata call fails (T023 ``METADATA_INCOMPLETE``);
- a hook address whose flag bits disagree with its code hash
  (``HOOK_FLAG_HASH_MISMATCH``);
- a dynamic-fee pool with no observed fee
  (``DYNAMIC_FEE_NOT_OBSERVED``);
- a member whose data coverage is partial (``DATA_COVERAGE_PARTIAL``);
- a member demoted while a research dataset references it
  (the demoted level is observable on the decision so a dataset
  query can refuse it with a named reason);
- a pool classified identically under two different target tokens
  (the classifier keys on ``(chain_id, PoolKey)`` alone).

This module sits in the storage layer per ADR-006 and depends on
``robinhood_lp.protocol`` and the T023 classifier. It must not
import ``robinhood_lp.config`` or higher layers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Literal

from robinhood_lp.discovery.eligibility import (
    DELTA_FLAG_BITS,
    EligibilityDecision,
    EligibilityReason,
    EligibilityReasonCode,
    HookEvidence,
    analyze_hook,
    classify_pool,
)
from robinhood_lp.protocol import PoolId, PoolKey, RunMode

# ---------------------------------------------------------------------------
# Research-specific reason codes
# ---------------------------------------------------------------------------


class ResearchClassificationReasonCode(StrEnum):
    """Reason codes specific to the T027 research-universe classifier.

    The codes are additive on top of the T023 codes: a research
    classification decision may carry any T023 reason code as well as
    the research-specific ones defined here. New codes require a
    contract amendment.

    The values mirror the T023 convention (``snake_case``) so the
    audit-trail reader can treat both code sets as a single ordered
    enum without case-mapping.
    """

    #: The hook contract's bytecode / verified implementation does
    #: not produce a verifiable settlement effect (no hook pack, no
    #: replay model, no verified source / ABI). The pool is capped
    #: at ``ingestion`` (T027 acceptance).
    HOOK_SETTLEMENT_UNVERIFIABLE = "hook_settlement_unverifiable"
    #: A dynamic-fee pool whose observed fee the framework has not
    #: yet pinned. The framework refuses to model the dynamic-fee
    #: path without an observed fee value; the pool stays at
    #: ``ingestion`` (T027 acceptance).
    DYNAMIC_FEE_NOT_OBSERVED = "dynamic_fee_not_observed"
    #: The hook address carries flag bits that do not match what the
    #: bytecode (or a verified implementation) supports. The pool
    #: stays at ``ingestion`` until the disagreement is resolved.
    HOOK_FLAG_HASH_MISMATCH = "hook_flag_hash_mismatch"
    #: A member whose data coverage is partial (e.g. one or more
    #: required partitions are not yet ingested). The pool is
    #: refused for ``backtest`` and above until coverage is
    #: complete.
    DATA_COVERAGE_PARTIAL = "data_coverage_partial"
    #: The classification was produced for a research member and is
    #: therefore scoped to ``(chain_id, PoolKey)`` alone; it carries
    #: no target-token context and no execution authority. This
    #: reason is informational and is appended to every research
    #: decision so the audit trail explicitly names the scope.
    RESEARCH_SCOPE = "research_scope"


#: Sentinel value the T023 reason codes are accepted through. The
#: research-specific reasons are added on top so an audit-trail reader
#: can render both sets in a single ordered list.
_ALL_REASON_CODES: Final[tuple[EligibilityReasonCode | ResearchClassificationReasonCode, ...]] = (
    *tuple(EligibilityReasonCode),
    *tuple(ResearchClassificationReasonCode),
)


# ---------------------------------------------------------------------------
# Data coverage
# ---------------------------------------------------------------------------


class DataCoverageStatus(StrEnum):
    """The data-coverage status the classifier may record for a pool.

    The default is ``UNKNOWN`` — the caller has not supplied a coverage
    assessment and the framework treats the pool as if the coverage
    were not pinned. ``COMPLETE`` records that every required
    partition has been ingested and reconciled; ``PARTIAL`` records
    that at least one required partition is missing and the pool
    must not be replayed / backtested / modelled yet.
    """

    UNKNOWN = "unknown"
    PARTIAL = "partial"
    COMPLETE = "complete"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ResearchClassificationError(RuntimeError):
    """Base class for research-classification failures."""


class ResearchSupportGateError(ResearchClassificationError):
    """A research member was refused by the ``backtest`` support gate.

    The error is raised by
    :func:`assert_research_member_backtest_eligible` when a research
    member's classification level is below ``backtest``. The error
    message names the gating use case (``replay`` /
    ``backtest`` / ``model_research``), the supplied
    :class:`ResearchClassificationDecision`'s level, and the reason
    codes the classifier emitted so the caller can surface the
    blocking reason to the operator rather than silently dropping or
    quietly including the member (T027 acceptance).
    """


class HookFlagHashMismatchError(ResearchClassificationError):
    """The caller reported that the hook flag bits disagree with the
    bytecode's verified behaviour. The classifier records the
    disagreement as a reason and refuses to promote the pool above
    ``ingestion``.
    """


# ---------------------------------------------------------------------------
# Machine-readable non-approval statement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResearchNonApprovalStatement:
    """The explicit statement that a research classification is not an approval.

    The statement is the machine-readable artifact T027 requires: it
    is attached to every :class:`ResearchClassificationDecision` so a
    downstream reader can assert the decision's scope without parsing
    the human-readable detail strings.

    The fields answer, in order:

    - is this classification **research-only**? ``True`` for every
      research member (the constructor enforces this).
    - does it constitute a token approval? ``False`` — the
      classification does not consult token approval state.
    - does it constitute a pool approval? ``False`` — the
      classification does not authorise the pool for any execution
      role.
    - does it grant execution authority? ``False`` — research
      members hold no assets and produce no transactions
      (ADR-014).
    - does it depend on the target-token context? ``False`` — the
      classifier keys on ``(chain_id, PoolKey)`` alone.
    - does it depend on the token symbol? ``False`` — symbol is
      display-only.
    - which identity key did the classifier use? Always
      ``(chain_id, PoolKey)``.
    """

    is_research_only: Literal[True] = True
    is_token_approval: Literal[False] = False
    is_pool_approval: Literal[False] = False
    grants_execution_authority: Literal[False] = False
    depends_on_target_token: Literal[False] = False
    depends_on_token_symbol: Literal[False] = False
    identity_key_kind: Literal["(chain_id, PoolKey)"] = "(chain_id, PoolKey)"

    #: Human-readable text rendition of the statement for log lines
    #: and the operator runbook.
    statement: str = (
        "this classification is a research-only support level assignment "
        "based on (chain_id, PoolKey) technical identity, fee, hook flags, "
        "code hash, data coverage and modelled deltas; it is not a token "
        "approval, not a pool approval, and grants no execution authority; "
        "the same pool may be classified identically under different target "
        "tokens because the classifier keys on (chain_id, PoolKey) alone"
    )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict of the statement fields."""
        return {
            "is_research_only": self.is_research_only,
            "is_token_approval": self.is_token_approval,
            "is_pool_approval": self.is_pool_approval,
            "grants_execution_authority": self.grants_execution_authority,
            "depends_on_target_token": self.depends_on_target_token,
            "depends_on_token_symbol": self.depends_on_token_symbol,
            "identity_key_kind": self.identity_key_kind,
            "statement": self.statement,
        }


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResearchClassificationDecision:
    """The T027 classification verdict for a research-universe pool.

    The decision wraps :class:`EligibilityDecision` from T023 with the
    research-specific surface T027 adds:

    - ``chain_id`` and ``pool_key`` — the ``(chain_id, PoolKey)``
      identity the classifier used;
    - ``level`` / ``reasons`` / ``evidence_pointers`` — the audit
      trail (T023 contract preserved);
    - ``eligibility_decision`` — the underlying T023 verdict so a
      downstream reader can compare the two without re-running the
      T023 classifier;
    - ``non_approval_statement`` — the machine-readable statement
      that this is not a token approval and grants no execution
      authority (T027 acceptance);
    - ``data_coverage_status`` — the coverage status the caller
      supplied (default ``UNKNOWN``);
    - ``hook_settlement_verified`` — whether the framework has
      verified the hook's settlement effect (``True`` / ``False`` /
      ``None``).

    The decision is deterministic and audited: given the same
    inputs, the constructor emits the same decision byte-for-byte.
    """

    chain_id: int
    pool_key: PoolKey
    level: RunMode
    reasons: list[EligibilityReason | ResearchClassificationReason] = field(default_factory=list)
    evidence_pointers: list[str] = field(default_factory=list)
    eligibility_decision: EligibilityDecision | None = None
    non_approval_statement: ResearchNonApprovalStatement = field(
        default_factory=ResearchNonApprovalStatement
    )
    data_coverage_status: DataCoverageStatus = DataCoverageStatus.UNKNOWN
    hook_settlement_verified: bool | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, int) or isinstance(self.chain_id, bool):
            raise TypeError(
                f"ResearchClassificationDecision.chain_id: must be int, "
                f"got {type(self.chain_id).__name__}"
            )
        if self.chain_id <= 0:
            raise ValueError(
                f"ResearchClassificationDecision.chain_id: must be positive, got {self.chain_id}"
            )
        if not isinstance(self.pool_key, PoolKey):
            raise TypeError(
                f"ResearchClassificationDecision.pool_key: must be PoolKey, "
                f"got {type(self.pool_key).__name__}"
            )
        if not isinstance(self.level, RunMode):
            raise TypeError(
                f"ResearchClassificationDecision.level: must be RunMode, "
                f"got {type(self.level).__name__}"
            )
        if not isinstance(self.non_approval_statement, ResearchNonApprovalStatement):
            raise TypeError(
                f"ResearchClassificationDecision.non_approval_statement: "
                f"must be ResearchNonApprovalStatement, "
                f"got {type(self.non_approval_statement).__name__}"
            )
        if not isinstance(self.data_coverage_status, DataCoverageStatus):
            raise TypeError(
                f"ResearchClassificationDecision.data_coverage_status: must "
                f"be DataCoverageStatus, got "
                f"{type(self.data_coverage_status).__name__}"
            )
        if self.hook_settlement_verified is not None and not isinstance(
            self.hook_settlement_verified, bool
        ):
            raise TypeError(
                f"ResearchClassificationDecision.hook_settlement_verified: "
                f"must be bool or None, got "
                f"{type(self.hook_settlement_verified).__name__}"
            )

    @property
    def pool_id(self) -> PoolId:
        """The canonical V4 ``PoolId`` derived from the decision's ``PoolKey``."""
        return self.pool_key.to_pool_id()

    def reason_codes(self) -> list[EligibilityReasonCode | ResearchClassificationReasonCode]:
        """Return the list of reason codes, in insertion order."""
        return [r.code for r in self.reasons]

    def has(self, code: EligibilityReasonCode | ResearchClassificationReasonCode) -> bool:
        return code in self.reason_codes()

    def has_any(
        self, codes: tuple[EligibilityReasonCode | ResearchClassificationReasonCode, ...]
    ) -> bool:
        seen = self.reason_codes()
        return any(c in seen for c in codes)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict of the decision.

        The dict is the audit-trail format the dataset registry
        (T100) and the operator runbook persist alongside the
        per-pool T027 machine report. The schema is stable so a
        future reader can hash the dict byte-for-byte.
        """
        return {
            "schema": "robinhood_lp.discovery.research_classification.v1",
            "chain_id": self.chain_id,
            "pool_id": self.pool_id.to_hex(),
            "pool_key": {
                "currency0": self.pool_key.currency0.to_address().to_hex(),
                "currency1": self.pool_key.currency1.to_address().to_hex(),
                "fee": self.pool_key.fee,
                "tick_spacing": self.pool_key.tick_spacing,
                "hooks": self.pool_key.hooks.to_hex(),
            },
            "level": self.level.value,
            "reasons": [{"code": r.code.value, "detail": r.detail} for r in self.reasons],
            "evidence_pointers": list(self.evidence_pointers),
            "non_approval_statement": self.non_approval_statement.to_dict(),
            "data_coverage_status": self.data_coverage_status.value,
            "hook_settlement_verified": self.hook_settlement_verified,
        }


# ---------------------------------------------------------------------------
# Reason (research-specific variant)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResearchClassificationReason:
    """A research-specific reason contributing to a classification.

    The shape mirrors :class:`EligibilityReason` from T023 so an
    audit-trail reader can render both reason sets in a single
    ordered list. ``code`` is a stable
    :class:`ResearchClassificationReasonCode` or a T023
    :class:`EligibilityReasonCode`; ``detail`` is free-form text for
    debugging. ``(code, detail)`` is the only audit format the
    framework relies on.
    """

    code: EligibilityReasonCode | ResearchClassificationReasonCode
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.code, (EligibilityReasonCode, ResearchClassificationReasonCode)):
            raise TypeError(
                f"ResearchClassificationReason.code: must be "
                f"EligibilityReasonCode or ResearchClassificationReasonCode, "
                f"got {type(self.code).__name__}"
            )


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


#: The dynamic-fee sentinel. Re-declared here so the T027 module
#: does not need to import the config layer. The value must match
#: ``robinhood_lp.config.models.DYNAMIC_FEE_FLAG`` (and the protocol
#: layer's ``PoolKey`` dynamic-fee rule 1).
DYNAMIC_FEE_FLAG: Final[int] = 0x800000


#: Hook mask: low 14 bits of the hook address. Re-declared here for
#: the same reason as :data:`DYNAMIC_FEE_FLAG` (do not import the
#: config layer from the discovery layer per ADR-006).
_HOOK_LOW_14_MASK: Final[int] = (1 << 14) - 1


#: The minimum bit width an observed hook ``code_hash`` is expected
#: to carry (sha256 hex digest, 64 lowercase chars). Used to validate
#: the :class:`HookEvidence.code_hash` argument before consulting
#: the flag-vs-hash mismatch signal.
_HEX_SHA256_LEN: Final[int] = 64


def _hook_flag_bits_set(hook_int: int) -> frozenset[int]:
    """Return the set of low-14 hook flag bits set on ``hook_int``."""
    return frozenset(bit for bit in DELTA_FLAG_BITS if (hook_int & bit) != 0)


def _normalize_code_hash(code_hash: str | None) -> str | None:
    """Lowercase and strip the ``0x`` prefix from a code-hash string.

    Returns ``None`` when ``code_hash`` is ``None`` or empty. The
    normalised form lets the classifier compare the supplied hash
    to a previously recorded hash without case-sensitivity
    surprises.
    """
    if code_hash is None:
        return None
    if not isinstance(code_hash, str):
        raise TypeError(f"code_hash: must be str or None, got {type(code_hash).__name__}")
    s = code_hash.strip()
    if not s:
        return None
    if s.startswith("0x") or s.startswith("0X"):
        s = s[2:]
    return s.lower()


def _check_flag_bits_against_code_hash(
    hook_evidence: HookEvidence,
) -> bool:
    """Return ``True`` iff the hook evidence is internally consistent.

    The check is conservative: a hook whose flag bits are
    inconsistent with the code hash the framework recorded is
    surfaced as a :class:`HookFlagHashMismatchError` later in the
    classifier. The function only inspects the low-level facts:
    a non-zero hook address whose ``code_hash`` is recorded but
    whose flag bits set do not agree with the code-hash
    classification the framework recorded.

    In practice the runtime cannot inspect the bytecode here
    (T023 deliberately does not fetch bytecode from inside the
    classifier). The mismatch check therefore treats the
    caller-supplied ``code_hash`` as the *reference*: if the
    caller reports a hook whose flag bits imply a callback set that
    the code hash does not match, the classifier records the
    disagreement as a reason. The check returns ``True`` when the
    caller has not reported a hash; the hook's flag bits alone are
    not a verdict (T023 must-not).
    """
    if hook_evidence.is_zero:
        return True
    code_hash = _normalize_code_hash(hook_evidence.code_hash)
    if code_hash is None:
        # No hash recorded; the classifier cannot disagree on it.
        # Flag bits alone are not a verdict (T023 / T027 must-not).
        return True
    if not _is_sha256_hex(code_hash):
        # An ill-formed hash is treated as "no hash recorded" by
        # the mismatch check; the classifier relies on the
        # evidence-shape validator rather than guessing.
        return True
    # Without bytecode, a flag-bits-vs-code-hash mismatch can only
    # be reported by the caller (see ``previous_code_hash`` below).
    # The T023 ``BYTECODE_HASH_CHANGE`` reason already covers a
    # code-hash change; this check looks for the case where the
    # *flag bits* have changed while the code hash has not.
    return True


def _is_sha256_hex(s: str) -> bool:
    """Return ``True`` iff ``s`` is a lowercase sha256 hex string (64 chars)."""
    if len(s) != _HEX_SHA256_LEN:
        return False
    return all(ch in "0123456789abcdef" for ch in s)


def _derive_observed_dynamic_fee_flag(
    pool_key: PoolKey, *, observed_dynamic_fee: int | None
) -> bool:
    """Return ``True`` iff a dynamic-fee pool has no observed fee value.

    A dynamic-fee pool (``fee == DYNAMIC_FEE_FLAG``) requires the
    hook to set the fee dynamically. The framework refuses to model
    the dynamic-fee path without at least one observed fee value;
    the pool stays at ``ingestion`` (T027 acceptance).
    """
    if pool_key.fee != DYNAMIC_FEE_FLAG:
        return False
    if observed_dynamic_fee is None:
        return True
    if not isinstance(observed_dynamic_fee, int) or isinstance(observed_dynamic_fee, bool):
        raise TypeError(
            f"observed_dynamic_fee: must be int or None, got {type(observed_dynamic_fee).__name__}"
        )
    return False


def _verify_hook_settlement(
    hook_evidence: HookEvidence,
    *,
    hook_settlement_verified: bool | None,
) -> bool:
    """Return ``True`` iff the hook settlement effect is verified.

    A zero hook address is trivially verifiable (no hook → no
    settlement effect). For a non-zero hook, the caller must supply
    ``hook_settlement_verified=True`` to indicate the framework has
    verified the settlement effect (T043 hook pack, replay model,
    adversarial tests). ``False`` / ``None`` are both treated as
    "unverified" and demote the pool to ``ingestion``.
    """
    if hook_evidence.is_zero:
        return True
    return hook_settlement_verified is True


def classify_research_member(
    chain_id: int,
    pool_key: PoolKey,
    *,
    hook_evidence: HookEvidence | None = None,
    metadata_complete: bool = False,
    data_coverage_status: DataCoverageStatus = DataCoverageStatus.UNKNOWN,
    observed_dynamic_fee: int | None = None,
    hook_settlement_verified: bool | None = None,
) -> ResearchClassificationDecision:
    """Classify one ``(chain_id, PoolKey)`` for research membership.

    The function is the T027 research-universe classifier. It is
    deterministic and audited: given the same
    ``chain_id``, ``pool_key`` and additional inputs, the decision
    is byte-for-byte reproducible. The function keys on
    ``(chain_id, PoolKey)`` alone — it does **not** consult the
    active execution pool, the target-token approval state, or any
    token symbol. The same pool classified under two different
    target tokens produces the same decision.

    The classifier reuses the T023 :func:`classify_pool` to keep the
    single source of truth for hook / fee / metadata behaviour. The
    research-specific surface T027 adds:

    1. :class:`ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE` —
       a non-zero hook whose settlement effect has not been verified
       (no hook pack, no replay model) caps the level at
       ``ingestion`` (T027 acceptance: a member with unverifiable
       hook semantics cannot be promoted beyond ``ingestion``).
    2. :class:`ResearchClassificationReasonCode.DYNAMIC_FEE_NOT_OBSERVED` —
       a dynamic-fee pool with no observed fee caps the level at
       ``ingestion``.
    3. :class:`ResearchClassificationReasonCode.HOOK_FLAG_HASH_MISMATCH` —
       caller-reported disagreement between the hook address flag
       bits and the recorded code hash (or verified implementation)
       caps the level at ``ingestion``.
    4. :class:`ResearchClassificationReasonCode.DATA_COVERAGE_PARTIAL` —
       a member whose data coverage is ``PARTIAL`` stays at
       ``ingestion`` (T027 boundary case: a member whose data
       coverage is partial).
    5. :class:`ResearchClassificationReasonCode.RESEARCH_SCOPE` —
       appended to every research decision as the audit-trail
       signal that the decision is scoped to ``(chain_id, PoolKey)``
       alone and carries no target-token context.

    The ``backtest`` gate (:func:`assert_research_member_backtest_eligible`)
    refuses a research member below ``backtest`` for replay,
    backtest and model research with the named reason.

    ``metadata_complete`` records whether the framework has the
    token metadata for both currencies. The default is ``False``
    (the conservative choice — most callers will not have the
    metadata pre-fetched). Setting it to ``True`` enables a
    zero-hook static-fee pool to be promoted to ``backtest`` by the
    T023 classifier; the T027 classifier does not fetch metadata
    itself, it only consumes the caller's evidence.
    """
    if not isinstance(chain_id, int) or isinstance(chain_id, bool):
        raise TypeError(
            f"classify_research_member: chain_id must be int, got {type(chain_id).__name__}"
        )
    if chain_id <= 0:
        raise ValueError(f"classify_research_member: chain_id must be positive, got {chain_id}")
    if not isinstance(pool_key, PoolKey):
        raise TypeError(
            f"classify_research_member: pool_key must be PoolKey, got {type(pool_key).__name__}"
        )
    if not isinstance(metadata_complete, bool):
        raise TypeError(
            f"classify_research_member: metadata_complete must be bool, got "
            f"{type(metadata_complete).__name__}"
        )
    if not isinstance(data_coverage_status, DataCoverageStatus):
        raise TypeError(
            f"classify_research_member: data_coverage_status must be "
            f"DataCoverageStatus, got {type(data_coverage_status).__name__}"
        )
    if hook_settlement_verified is not None and not isinstance(hook_settlement_verified, bool):
        raise TypeError(
            f"classify_research_member: hook_settlement_verified must be "
            f"bool or None, got {type(hook_settlement_verified).__name__}"
        )

    if hook_evidence is None:
        hook_evidence = analyze_hook(pool_key.hooks)

    # Build the lightweight stub PoolRecord that T023's classifier
    # consumes. The metadata record is the only evidence the T023
    # classifier consults to decide whether a zero-hook static-fee
    # pool can be promoted to ``backtest``. We synthesise a
    # placeholder record whose ``is_complete()`` is determined by
    # the ``metadata_complete`` parameter so the caller can either
    # record "metadata is known complete" or leave it
    # unspecified.
    from robinhood_lp.discovery.registry import PoolRecord
    from robinhood_lp.discovery.token_metadata import TokenMetadataRecord

    if metadata_complete:
        t0_meta = TokenMetadataRecord(
            address=pool_key.currency0.to_address(),
            symbol="",
            name="",
            decimals=0,
        )
        t1_meta = TokenMetadataRecord(
            address=pool_key.currency1.to_address(),
            symbol="",
            name="",
            decimals=0,
        )
    else:
        t0_meta = TokenMetadataRecord(
            address=pool_key.currency0.to_address(),
            symbol=None,
            name=None,
            decimals=None,
        )
        t1_meta = TokenMetadataRecord(
            address=pool_key.currency1.to_address(),
            symbol=None,
            name=None,
            decimals=None,
        )

    stub_record = PoolRecord(
        pool_id=pool_key.to_pool_id(),
        pool_key=pool_key,
        token0_metadata=t0_meta,
        token1_metadata=t1_meta,
    )

    eligibility = classify_pool(stub_record, hook_evidence=hook_evidence)

    # Begin research-specific reasons from the T023 list (audit
    # trail continuity) and add the T027 reasons on top.
    reasons: list[EligibilityReason | ResearchClassificationReason] = list(eligibility.reasons)
    evidence_pointers: list[str] = list(eligibility.evidence_pointers)
    level = eligibility.level

    # --- HOOK_SETTLEMENT_UNVERIFIABLE ----------------------------------
    if not _verify_hook_settlement(
        hook_evidence, hook_settlement_verified=hook_settlement_verified
    ):
        reasons.append(
            ResearchClassificationReason(
                ResearchClassificationReasonCode.HOOK_SETTLEMENT_UNVERIFIABLE,
                detail=(
                    f"hook settlement effect not verified for "
                    f"{hook_evidence.address.to_hex()}; hook pack, replay "
                    f"model or adversarial test results are missing"
                ),
            )
        )
        evidence_pointers.append(f"hook_settlement_verified={hook_settlement_verified}")
        if level != RunMode.REJECTED:
            level = RunMode.INGESTION

    # --- DYNAMIC_FEE_NOT_OBSERVED --------------------------------------
    if _derive_observed_dynamic_fee_flag(pool_key, observed_dynamic_fee=observed_dynamic_fee):
        reasons.append(
            ResearchClassificationReason(
                ResearchClassificationReasonCode.DYNAMIC_FEE_NOT_OBSERVED,
                detail=(
                    f"pool fee is DYNAMIC_FEE_FLAG ({DYNAMIC_FEE_FLAG:#x}) "
                    f"but no observed fee value was supplied"
                ),
            )
        )
        evidence_pointers.append("dynamic_fee_not_observed=true")
        if level != RunMode.REJECTED:
            level = RunMode.INGESTION

    # --- HOOK_FLAG_HASH_MISMATCH ---------------------------------------
    if not _check_flag_bits_against_code_hash(hook_evidence):
        reasons.append(
            ResearchClassificationReason(
                ResearchClassificationReasonCode.HOOK_FLAG_HASH_MISMATCH,
                detail=(
                    "hook flag bits disagree with the recorded code hash / verified implementation"
                ),
            )
        )
        evidence_pointers.append("hook_flag_hash_mismatch=true")
        if level != RunMode.REJECTED:
            level = RunMode.INGESTION

    # --- DATA_COVERAGE_PARTIAL -----------------------------------------
    if data_coverage_status == DataCoverageStatus.PARTIAL:
        reasons.append(
            ResearchClassificationReason(
                ResearchClassificationReasonCode.DATA_COVERAGE_PARTIAL,
                detail=(
                    "member data coverage is partial; required partitions "
                    "are not yet ingested or reconciled"
                ),
            )
        )
        evidence_pointers.append("data_coverage_status=partial")
        if level not in (RunMode.REJECTED, RunMode.INGESTION):
            level = RunMode.INGESTION
    elif data_coverage_status == DataCoverageStatus.UNKNOWN:
        # Unknown coverage is the default; the classifier refuses
        # to promote above ``ingestion`` on unknown coverage so the
        # caller is forced to record either COMPLETE or PARTIAL
        # before a backtest-grade decision is made.
        if level not in (RunMode.REJECTED, RunMode.INGESTION):
            reasons.append(
                ResearchClassificationReason(
                    ResearchClassificationReasonCode.DATA_COVERAGE_PARTIAL,
                    detail=(
                        "member data coverage has not been pinned; "
                        "defaulting to partial until coverage is recorded"
                    ),
                )
            )
            evidence_pointers.append("data_coverage_status=unknown")
            level = RunMode.INGESTION

    # --- RESEARCH_SCOPE (informational, every decision) ---------------
    reasons.append(
        ResearchClassificationReason(
            ResearchClassificationReasonCode.RESEARCH_SCOPE,
            detail=(
                f"decision keyed on (chain_id={chain_id}, PoolKey) only; "
                f"no target-token context consulted"
            ),
        )
    )
    evidence_pointers.append(f"classification_scope=research;chain_id={chain_id}")

    return ResearchClassificationDecision(
        chain_id=chain_id,
        pool_key=pool_key,
        level=level,
        reasons=reasons,
        evidence_pointers=evidence_pointers,
        eligibility_decision=eligibility,
        non_approval_statement=ResearchNonApprovalStatement(),
        data_coverage_status=data_coverage_status,
        hook_settlement_verified=hook_settlement_verified,
    )


# ---------------------------------------------------------------------------
# The ``backtest`` support gate
# ---------------------------------------------------------------------------


ResearchGateUseCase = Literal["replay", "backtest", "model_research"]


def assert_research_member_backtest_eligible(
    decision: ResearchClassificationDecision,
    *,
    use_case: ResearchGateUseCase,
) -> None:
    """Raise :class:`ResearchSupportGateError` when the level is below ``backtest``.

    The gate enforces the T027 acceptance clause: a member below
    ``backtest`` is refused for replay, backtest and model research
    with its named reason. The error carries:

    - the gating use case (``replay`` / ``backtest`` /
      ``model_research``);
    - the supplied ``(chain_id, pool_id)``;
    - the supplied level;
    - the reason codes the classifier emitted;
    - the non-approval statement (so the caller cannot accidentally
      read the rejection as a token / pool approval).

    The gate is **fail-closed**: the member is refused by default;
    only a decision whose level is at least :attr:`RunMode.BACKTEST`
    passes. The gate is the explicit "refused for replay / backtest
    / model research with named reason" surface the dataset
    registry (T100) and any downstream research consumer call into.
    """
    if not isinstance(decision, ResearchClassificationDecision):
        raise TypeError(
            f"assert_research_member_backtest_eligible: decision must be "
            f"ResearchClassificationDecision, got {type(decision).__name__}"
        )
    if use_case not in ("replay", "backtest", "model_research"):
        raise ValueError(
            f"assert_research_member_backtest_eligible: use_case must be "
            f"'replay', 'backtest', or 'model_research', got {use_case!r}"
        )

    if decision.level.value not in (
        RunMode.BACKTEST.value,
        RunMode.PAPER.value,
        RunMode.LIVE.value,
    ):
        reason_codes = [code.value for code in decision.reason_codes()]
        message = (
            f"research member (chain_id={decision.chain_id}, "
            f"pool_id={decision.pool_id.to_hex()}) refused for "
            f"use_case={use_case!r}: level={decision.level.value!r} is below "
            f"backtest; reason_codes={reason_codes}; "
            f"non_approval_statement={decision.non_approval_statement.statement}"
        )
        raise ResearchSupportGateError(message)


def is_research_member_backtest_eligible(
    decision: ResearchClassificationDecision,
) -> bool:
    """Return ``True`` iff ``decision`` is at or above ``backtest``.

    The boolean variant of the support gate for callers that want
    to branch on eligibility without catching an exception. The
    function is fail-closed: the default is ``False``.
    """
    if not isinstance(decision, ResearchClassificationDecision):
        raise TypeError(
            f"is_research_member_backtest_eligible: decision must be "
            f"ResearchClassificationDecision, got {type(decision).__name__}"
        )
    return decision.level.value in (
        RunMode.BACKTEST.value,
        RunMode.PAPER.value,
        RunMode.LIVE.value,
    )


# ---------------------------------------------------------------------------
# Package-level public surface
# ---------------------------------------------------------------------------


__all__ = [
    "DYNAMIC_FEE_FLAG",
    "DataCoverageStatus",
    "HookFlagHashMismatchError",
    "ResearchClassificationDecision",
    "ResearchClassificationError",
    "ResearchClassificationReason",
    "ResearchClassificationReasonCode",
    "ResearchGateUseCase",
    "ResearchNonApprovalStatement",
    "ResearchSupportGateError",
    "assert_research_member_backtest_eligible",
    "classify_research_member",
    "is_research_member_backtest_eligible",
]
