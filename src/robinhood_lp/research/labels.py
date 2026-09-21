"""Forward-label schema the panel harness reads (T101, DS-011).

The label module names the closed vocabulary the panel uses for
forward-looking labels. Every label carries four pieces of metadata
``DS-011`` requires:

- **horizon** — the forward distance the label spans, expressed
  in the same kind (``TIME`` or ``BLOCK``) the bar uses;
- **observability moment** — the timestamp or block number after
  which the label may be consulted; this is ``decision_time +
  horizon`` for a clean horizon;
- **unit** — the integer / Q64.64 unit the label's value carries
  before the boundary crossing in ``robinhood_lp.research.boundary``
  reduces it to ``float``;
- **role** — what the label is for (``target`` for the panel's
  primary prediction target, ``auxiliary`` for a feature that is
  only used during diagnostics).

The contract names six labels:

- realized forward volatility;
- realized forward volume;
- realized forward fee density;
- the loss-versus-rebalancing proxy (LP_METRICS ``M-LVR-001``);
- the probability that price exits a candidate range;
- the net LP return (the publicising target every label is *not*).

The model layer estimates these decision inputs, not the return
itself. ``Net LP return`` is included as a label that the panel
records for the diagnostic comparison but that no model in
``DS-030``'s family estimates; including it in the schema binds
the harness to the truth that the return is the model's
*symptom*, not the model's target.

The label values are integers (or ``None`` for missing-data
sentinels). The boundary crossing happens at the model layer per
the statistical boundary the panel declares. Each label has its
own closed-vocabulary unit and role; renaming or removing a
label is a contract change because the harness names it on the
model artifact.

References:

- ``docs/spec/research/DATASET_AND_EVALUATION.md`` §3 (``DS-011``);
- ``docs/spec/strategy/LP_METRICS.md`` ``M-LVR-001`` and the
  reference ``M-BM-002`` that defines the proxy basis;
- ``docs/spec/strategy/STRATEGY_ECONOMICS.md``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from robinhood_lp.features.bars import WindowKind
from robinhood_lp.features.quote import ObservationUnit

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LabelSchemaError(ValueError):
    """Base class for label-schema failures."""


class UnknownLabelError(LabelSchemaError):
    """A label name the harness requested has no schema entry."""


class InvalidLabelHorizonError(LabelSchemaError):
    """A label's horizon is non-positive or its window is invalid."""


class InvalidLabelObservabilityError(LabelSchemaError):
    """A label's observability moment precedes its decision time."""


class LabelRoleMismatchError(LabelSchemaError):
    """A caller asked for a label under a role different from the schema's."""


class LabelSizeOverflowError(LabelSchemaError):
    """Too few samples for a quantile level to estimate, per ``DS-032``."""


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


class LabelRole(StrEnum):
    """The role a label plays in the panel.

    - :attr:`TARGET` — the primary prediction target the model
      estimates at training time; the contract names three
      targets (``EXIT_PROBABILITY``, ``REALIZED_VOL``,
      ``FEE_DENSITY``).
    - :attr:`AUXILIARY` — a label that appears in the schema for
      diagnostics or feature cross-checking but is **never** a
      model target (e.g. realized volume and realised LVR proxy).
    - :attr:`OUTCOME` — the outcome label the harness records
      only for the diagnostic comparison (net LP return). The
      outcome label is **not** a model target by design, per
      ``DS-040`` and ``ADR-014`` §5 (model estimates decision
      inputs, not returns).
    """

    TARGET = "TARGET"
    AUXILIARY = "AUXILIARY"
    OUTCOME = "OUTCOME"


class LabelKind(StrEnum):
    """The semantic kind a label name refers to.

    The closed vocabulary mirrors the six labels the T101
    contract names. Strings are part of the public contract.
    New kinds are additive; renaming an existing kind is a
    breaking change.
    """

    REALIZED_VOL = "REALIZED_VOL"
    REALIZED_VOLUME = "REALIZED_VOLUME"
    FEE_DENSITY = "FEE_DENSITY"
    LOSS_VS_REBAL_PROXY = "LOSS_VS_REBAL_PROXY"
    EXIT_PROBABILITY = "EXIT_PROBABILITY"
    NET_LP_RETURN = "NET_LP_RETURN"


# ---------------------------------------------------------------------------
# Required horizon for each label kind
# ---------------------------------------------------------------------------


#: Minimum horizon the schema enforces for each label kind.
#: Renaming or removing an entry is a contract change. The
#: minimum horizons match the T050 / T052 windows the panel
#: already uses (5-minute windows for realized vol / volume /
#: fee density and 1-hour windows for the LVR proxy).
DEFAULT_LABEL_HORIZON_MIN: Final[dict[str, int]] = {
    LabelKind.REALIZED_VOL.value: 300,
    LabelKind.REALIZED_VOLUME.value: 300,
    LabelKind.FEE_DENSITY.value: 300,
    LabelKind.LOSS_VS_REBAL_PROXY.value: 3600,
    LabelKind.EXIT_PROBABILITY.value: 300,
    LabelKind.NET_LP_RETURN.value: 3600,
}


#: The closed-vocabulary unit each label kind is recorded in.
DEFAULT_LABEL_UNIT: Final[dict[str, ObservationUnit]] = {
    LabelKind.REALIZED_VOL.value: ObservationUnit.RATIO,
    LabelKind.REALIZED_VOLUME.value: ObservationUnit.RAW_TOKEN_INTEGER,
    LabelKind.FEE_DENSITY.value: ObservationUnit.RATIO,
    LabelKind.LOSS_VS_REBAL_PROXY.value: ObservationUnit.RATIO,
    LabelKind.EXIT_PROBABILITY.value: ObservationUnit.DIMENSIONLESS,
    LabelKind.NET_LP_RETURN.value: ObservationUnit.RATIO,
}


#: Default window kind for each label kind.
DEFAULT_LABEL_WINDOW_KIND: Final[dict[str, WindowKind]] = {
    LabelKind.REALIZED_VOL.value: WindowKind.TIME,
    LabelKind.REALIZED_VOLUME.value: WindowKind.TIME,
    LabelKind.FEE_DENSITY.value: WindowKind.TIME,
    LabelKind.LOSS_VS_REBAL_PROXY.value: WindowKind.TIME,
    LabelKind.EXIT_PROBABILITY.value: WindowKind.TIME,
    LabelKind.NET_LP_RETURN.value: WindowKind.TIME,
}


# ---------------------------------------------------------------------------
# Quantile level
# ---------------------------------------------------------------------------


#: Allowed quantile levels the panel can record for a TARGET
#: label. Adding a new level is additive; renaming is a breaking
#: change. The set is the panel's quantiles (DS-033 requires a
#: quantile-coverage diagnostic).
DEFAULT_PANEL_QUANTILE_LEVELS: Final[tuple[float, ...]] = (0.1, 0.5, 0.9)


#: Minimum sample size at which a quantile level is reported.
#: ``DS-032`` requires an honest "sample too small" disclosure;
#: a label whose effective sample size is below this threshold
#: is reported as ``UNCERTAIN`` for that quantile rather than
#: estimated.
DEFAULT_QUANTILE_MIN_SAMPLE_SIZE: Final[int] = 30


# ---------------------------------------------------------------------------
# Label declaration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabelDeclaration:
    """One named label in the schema.

    The declaration is immutable; the schema's content hash is
    derived from the declaration sequence so a re-run validates
    that the live schema still produces the same digest.

    Field units:

    - ``label_name`` — non-empty string, unique within the schema.
    - ``kind`` — the closed-vocabulary label kind.
    - ``role`` — :class:`LabelRole`.
    - ``unit`` — :class:`ObservationUnit`; mirrors
      :data:`DEFAULT_LABEL_UNIT` by default.
    - ``window_kind`` — :class:`WindowKind`; mirrors
      :data:`DEFAULT_LABEL_WINDOW_KIND` by default.
    - ``horizon_seconds`` — non-negative integer; the forward
      distance the label spans, expressed in the same unit the
      panel uses (``seconds`` for ``TIME`` windows; ``blocks``
      for ``BLOCK`` windows).
    - ``observes_after_seconds`` — non-negative integer; the
      moment the label becomes observable, expressed in the
      same unit as the horizon. Must equal ``horizon_seconds``
      for a clean horizon or be strictly larger when the
      consumer is a ``COUNT_AS_GAP`` late-event record.
    - ``quantile_levels`` — tuple of floats in ``(0.0, 1.0]``
      for labels whose model estimates a distribution (TARGET,
      AUXILIARY, and OUTCOME). The default is the panel's
      three quantiles.

    Equality and hashing follow the dataclass identity.
    """

    label_name: str
    kind: LabelKind
    role: LabelRole
    unit: ObservationUnit
    window_kind: WindowKind
    horizon_seconds: int
    observes_after_seconds: int
    quantile_levels: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.label_name, str) or not self.label_name:
            raise LabelSchemaError(
                f"LabelDeclaration.label_name: must be non-empty str, got {self.label_name!r}"
            )
        if not isinstance(self.kind, LabelKind):
            raise LabelSchemaError(
                f"LabelDeclaration.kind: must be LabelKind, got {type(self.kind).__name__}"
            )
        if not isinstance(self.role, LabelRole):
            raise LabelSchemaError(
                f"LabelDeclaration.role: must be LabelRole, got {type(self.role).__name__}"
            )
        if not isinstance(self.unit, ObservationUnit):
            raise LabelSchemaError(
                f"LabelDeclaration.unit: must be ObservationUnit, got {type(self.unit).__name__}"
            )
        if not isinstance(self.window_kind, WindowKind):
            raise LabelSchemaError(
                f"LabelDeclaration.window_kind: must be WindowKind, "
                f"got {type(self.window_kind).__name__}"
            )
        if not isinstance(self.horizon_seconds, int) or isinstance(self.horizon_seconds, bool):
            raise LabelSchemaError(
                f"LabelDeclaration.horizon_seconds: must be int, "
                f"got {type(self.horizon_seconds).__name__}"
            )
        if self.horizon_seconds <= 0:
            raise InvalidLabelHorizonError(
                f"LabelDeclaration.horizon_seconds={self.horizon_seconds} must be > 0"
            )
        minimum = DEFAULT_LABEL_HORIZON_MIN[self.kind.value]
        if self.horizon_seconds < minimum:
            raise InvalidLabelHorizonError(
                f"LabelDeclaration.horizon_seconds={self.horizon_seconds} "
                f"is below the minimum {minimum} for label kind "
                f"{self.kind.value!r}"
            )
        if not isinstance(self.observes_after_seconds, int) or isinstance(
            self.observes_after_seconds, bool
        ):
            raise LabelSchemaError(
                f"LabelDeclaration.observes_after_seconds: must be int, "
                f"got {type(self.observes_after_seconds).__name__}"
            )
        if self.observes_after_seconds < self.horizon_seconds:
            raise InvalidLabelObservabilityError(
                f"LabelDeclaration.observes_after_seconds="
                f"{self.observes_after_seconds} must be >= horizon_seconds="
                f"{self.horizon_seconds}"
            )
        if not isinstance(self.quantile_levels, tuple):
            raise LabelSchemaError(
                f"LabelDeclaration.quantile_levels: must be tuple[float, ...], "
                f"got {type(self.quantile_levels).__name__}"
            )
        for level in self.quantile_levels:
            if not isinstance(level, float) or not 0.0 < level <= 1.0:
                raise LabelSchemaError(
                    f"LabelDeclaration.quantile_levels: every entry must be "
                    f"float in (0.0, 1.0], got {level!r}"
                )

    @property
    def is_target(self) -> bool:
        """``True`` iff the label is a model target (``TARGET`` role)."""
        return self.role is LabelRole.TARGET

    @property
    def is_outcome(self) -> bool:
        """``True`` iff the label is the diagnostic outcome (``OUTCOME`` role)."""
        return self.role is LabelRole.OUTCOME

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "label_name": self.label_name,
            "kind": self.kind.value,
            "role": self.role.value,
            "unit": self.unit.value,
            "window_kind": self.window_kind.value,
            "horizon_seconds": self.horizon_seconds,
            "observes_after_seconds": self.observes_after_seconds,
            "quantile_levels": list(self.quantile_levels),
        }


# ---------------------------------------------------------------------------
# Label schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LabelSchema:
    """The closed-vocabulary schema of the panel's forward labels.

    The schema enumerates every label the panel may record. Two
    schemas with the same declarations in the same insertion order
    produce identical content hashes so a model artifact and a
    re-run agree about the label set.
    """

    #: Schema version. Bumping this is a contract change.
    VERSION: Final[str] = "t101.label_schema.v1"
    declarations: tuple[LabelDeclaration, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.declarations, tuple):
            raise LabelSchemaError(
                f"LabelSchema.declarations: must be tuple, got {type(self.declarations).__name__}"
            )
        seen: set[str] = set()
        for declaration in self.declarations:
            if not isinstance(declaration, LabelDeclaration):
                raise LabelSchemaError(
                    f"LabelSchema.declarations: every entry must be "
                    f"LabelDeclaration, got {type(declaration).__name__}"
                )
            if declaration.label_name in seen:
                raise LabelSchemaError(
                    f"LabelSchema.declarations: duplicate label_name {declaration.label_name!r}"
                )
            seen.add(declaration.label_name)
        # At least one target label must be present (the
        # model needs a target to train on); the diagnostic
        # outcome label is encouraged but optional.
        target_count = sum(1 for d in self.declarations if d.is_target)
        if target_count == 0:
            raise LabelSchemaError("LabelSchema: must have at least one TARGET label")

    @property
    def target_names(self) -> tuple[str, ...]:
        """The ordered tuple of TARGET label names."""
        return tuple(d.label_name for d in self.declarations if d.is_target)

    @property
    def auxiliary_names(self) -> tuple[str, ...]:
        """The ordered tuple of AUXILIARY label names."""
        return tuple(d.label_name for d in self.declarations if d.role is LabelRole.AUXILIARY)

    @property
    def outcome_names(self) -> tuple[str, ...]:
        """The ordered tuple of OUTCOME label names."""
        return tuple(d.label_name for d in self.declarations if d.is_outcome)

    def get(self, label_name: str) -> LabelDeclaration:
        """Return the declaration for ``label_name``."""
        if not isinstance(label_name, str) or not label_name:
            raise LabelSchemaError(
                f"LabelSchema.get: label_name must be non-empty str, got {label_name!r}"
            )
        for declaration in self.declarations:
            if declaration.label_name == label_name:
                return declaration
        raise UnknownLabelError(f"LabelSchema.get: no label declared under {label_name!r}")

    def has(self, label_name: str) -> bool:
        """Return ``True`` iff ``label_name`` is declared."""
        return any(d.label_name == label_name for d in self.declarations)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "version": self.VERSION,
            "declarations": [d.to_dict() for d in self.declarations],
        }


# ---------------------------------------------------------------------------
# Default panel label schema
# ---------------------------------------------------------------------------


def default_panel_label_schema() -> LabelSchema:
    """Return the canonical panel label schema the harness starts from.

    The schema names the six labels the T101 contract enumerates,
    each with its recommended role and horizon. Renaming a
    declaration here is a contract change.
    """
    return LabelSchema(
        declarations=(
            # Target labels — the model estimates these directly.
            LabelDeclaration(
                label_name="exit_probability",
                kind=LabelKind.EXIT_PROBABILITY,
                role=LabelRole.TARGET,
                unit=ObservationUnit.DIMENSIONLESS,
                window_kind=WindowKind.TIME,
                horizon_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.EXIT_PROBABILITY.value],
                observes_after_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.EXIT_PROBABILITY.value],
                quantile_levels=DEFAULT_PANEL_QUANTILE_LEVELS,
            ),
            LabelDeclaration(
                label_name="realized_variance_q64_64",
                kind=LabelKind.REALIZED_VOL,
                role=LabelRole.TARGET,
                unit=ObservationUnit.RATIO,
                window_kind=WindowKind.TIME,
                horizon_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.REALIZED_VOL.value],
                observes_after_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.REALIZED_VOL.value],
                quantile_levels=DEFAULT_PANEL_QUANTILE_LEVELS,
            ),
            LabelDeclaration(
                label_name="fee_density_q64_64",
                kind=LabelKind.FEE_DENSITY,
                role=LabelRole.TARGET,
                unit=ObservationUnit.RATIO,
                window_kind=WindowKind.TIME,
                horizon_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.FEE_DENSITY.value],
                observes_after_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.FEE_DENSITY.value],
                quantile_levels=DEFAULT_PANEL_QUANTILE_LEVELS,
            ),
            # Auxiliary label — used as a feature for some models
            # and surfaced in diagnostics.
            LabelDeclaration(
                label_name="realized_volume_token0",
                kind=LabelKind.REALIZED_VOLUME,
                role=LabelRole.AUXILIARY,
                unit=ObservationUnit.RAW_TOKEN_INTEGER,
                window_kind=WindowKind.TIME,
                horizon_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.REALIZED_VOLUME.value],
                observes_after_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.REALIZED_VOLUME.value],
            ),
            LabelDeclaration(
                label_name="loss_vs_rebal_proxy_q64_64",
                kind=LabelKind.LOSS_VS_REBAL_PROXY,
                role=LabelRole.AUXILIARY,
                unit=ObservationUnit.RATIO,
                window_kind=WindowKind.TIME,
                horizon_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.LOSS_VS_REBAL_PROXY.value],
                observes_after_seconds=DEFAULT_LABEL_HORIZON_MIN[
                    LabelKind.LOSS_VS_REBAL_PROXY.value
                ],
            ),
            # Outcome label — diagnostic only. The model never
            # estimates this directly (DS-040 / ADR-014 §5).
            LabelDeclaration(
                label_name="net_lp_return_q64_64",
                kind=LabelKind.NET_LP_RETURN,
                role=LabelRole.OUTCOME,
                unit=ObservationUnit.RATIO,
                window_kind=WindowKind.TIME,
                horizon_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.NET_LP_RETURN.value],
                observes_after_seconds=DEFAULT_LABEL_HORIZON_MIN[LabelKind.NET_LP_RETURN.value],
            ),
        )
    )


def validate_sample_size_for_quantile(
    *,
    sample_size: int,
    quantile_level: float,
    minimum: int = DEFAULT_QUANTILE_MIN_SAMPLE_SIZE,
) -> None:
    """Raise :class:`LabelSizeOverflowError` if ``sample_size`` is too small.

    The check is the ``DS-032`` honest-failure clause the harness
    surfaces in its reports; a quantile that cannot be estimated
    with statistical stability is reported as ``UNCERTAIN`` rather
    than producing a noisy point estimate.

    The default minimum is :data:`DEFAULT_QUANTILE_MIN_SAMPLE_SIZE`;
    tests may pass a smaller value when exercising boundary cases.
    """
    if not isinstance(sample_size, int) or isinstance(sample_size, bool):
        raise LabelSchemaError(
            f"validate_sample_size_for_quantile: sample_size must be int, "
            f"got {type(sample_size).__name__}"
        )
    if sample_size < 0:
        raise LabelSchemaError(
            f"validate_sample_size_for_quantile: sample_size must be >= 0, got {sample_size}"
        )
    if not isinstance(quantile_level, float):
        raise LabelSchemaError(
            f"validate_sample_size_for_quantile: quantile_level must be "
            f"float, got {type(quantile_level).__name__}"
        )
    if not 0.0 < quantile_level <= 1.0:
        raise LabelSchemaError(
            f"validate_sample_size_for_quantile: quantile_level must be in "
            f"(0.0, 1.0], got {quantile_level}"
        )
    if not isinstance(minimum, int) or isinstance(minimum, bool):
        raise LabelSchemaError(
            f"validate_sample_size_for_quantile: minimum must be int, got {type(minimum).__name__}"
        )
    if sample_size < minimum:
        raise LabelSizeOverflowError(
            f"validate_sample_size_for_quantile: sample_size={sample_size} "
            f"below minimum={minimum} for quantile_level={quantile_level}"
        )


__all__ = [
    "DEFAULT_LABEL_HORIZON_MIN",
    "DEFAULT_LABEL_UNIT",
    "DEFAULT_LABEL_WINDOW_KIND",
    "DEFAULT_PANEL_QUANTILE_LEVELS",
    "DEFAULT_QUANTILE_MIN_SAMPLE_SIZE",
    "InvalidLabelHorizonError",
    "InvalidLabelObservabilityError",
    "LabelDeclaration",
    "LabelKind",
    "LabelRole",
    "LabelRoleMismatchError",
    "LabelSchema",
    "LabelSchemaError",
    "LabelSizeOverflowError",
    "UnknownLabelError",
    "default_panel_label_schema",
    "validate_sample_size_for_quantile",
]
