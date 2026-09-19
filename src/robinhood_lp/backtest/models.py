"""Liquidity / fee / gas / slippage / failure models (T061).

The backtest engine consumes five parameterised, deterministic,
versioned, unit-carrying models. Every model is a frozen dataclass
with a ``model_version`` string; bumping the version is the supported
way to evolve the model semantics, and the engine records the version
on every audit event so a later rerun can replay with an older
implementation.

The five models:

- :class:`LiquidityModel` — answers "what is the active liquidity at
  time ``t``?" The reference implementation is a constant depth; the
  engine uses it to detect ``in_range`` flips and to zero accrual when
  the price leaves the Range.
- :class:`FeeModel` — answers "what fee tier does the pool charge at
  time ``t``?" The reference implementation supports a static tier
  (raw ``int`` pips, scaled by ``PIP_SCALE``); the field is unit-carrying
  so a downstream contributor can extend it to dynamic fees without
  silently changing the unit convention.
- :class:`GasModel` — answers "how many native-gas units does this
  action consume?" The reference implementation returns a fixed value;
  the unit is *gas units*, an integer atomic unit.
- :class:`SlippageModel` — answers "what is the price impact, in basis
  points, for a notional of ``size_q64_64``?" The reference
  implementation returns a constant bps value (zero for the unit test
  path; non-zero for the slippage-fan-out path).
- :class:`FailureModel` — answers "does this intent succeed, get
  rejected, fill partially, or get delayed?" The reference
  implementation is a deterministic schedule: the engine asks the model
  ``classify(intent_kind, ...)`` and the model returns one of the
  closed :class:`FillOutcome` sentinels.

Design constraints (binding):

- **Integer-only.** Per ADR-004, no ``float`` appears in any model
  field. Ratios (slippage bps, fee pips) are integers; the
  ``Q64.64`` scale for notional lives in :mod:`robinhood_lp.strategy`.

- **Determinism.** The same model version with the same inputs always
  produces the same outputs in any process. The reference
  implementations do not consult wall-clock time or unseeded
  randomness.

- **Unit-carrying.** Every field carries an explicit unit (gas units,
  basis points, pips, Q64.64). The unit is documented in the dataclass
  docstring so a downstream consumer cannot silently change the
  convention.

- **Layer purity.** The module depends only on the standard library
  and the ``robinhood_lp.backtest.events`` primitives. It imports no
  RPC, storage, signing, execution, replay, or feature module.

References:

- ADR-004 — integer / decimal precision policy (binding).
- ADR-006 — dependency direction; backtest models live at the backtest
  layer.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, Protocol, runtime_checkable

#: Unit constants. Every value the engine consumes or produces carries
#: one of these units; the comment on each dataclass field names the unit
#: explicitly so a downstream contributor cannot silently change the
#: convention.

#: Pip scale used by fee tiers. A value of ``FEE_DENOMINATOR_PIPS`` is
#: "100 %"; smaller values are sub-pip fractions.
FEE_DENOMINATOR_PIPS: Final[int] = 1_000_000  # 100 % = 1_000_000 pips

#: Basis-point scale used by slippage and price impact. A value of
#: ``SLIPPAGE_DENOMINATOR_BPS`` is "100 %"; 1 bp = 0.01 %.
SLIPPAGE_DENOMINATOR_BPS: Final[int] = 10_000  # 100 % = 10_000 bps


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class ModelError(ValueError):
    """Base class for backtest model construction / query failures."""


class InvalidModelVersionError(ModelError):
    """A model ``model_version`` is empty or non-string."""


class InvalidUnitError(ModelError):
    """A model field carries a value outside its declared unit domain."""


class InvalidFillOutcomeError(ModelError):
    """A :class:`FailureModel` returned an outcome outside the closed set."""


# ---------------------------------------------------------------------------
# Liquidity model
# ---------------------------------------------------------------------------


#: Version of the reference :class:`LiquidityModel` implementation. Bump
#: when the semantics of :meth:`LiquidityModel.active_liquidity` change.
LIQUIDITY_MODEL_VERSION: Final[str] = "t061.liquidity_model.v1"


@runtime_checkable
class LiquidityModel(Protocol):
    """The interface for the backtest liquidity model.

    A :class:`LiquidityModel` answers "what is the pool's active
    liquidity at time ``t``?" The engine uses the answer to detect
    ``in_range`` flips and to bound slippage / fill arithmetic.
    """

    @property
    def model_version(self) -> str:
        """The version string of this model instance."""
        ...

    def active_liquidity(self, *, pool_key_id: str, chain_id: int, event_time: int) -> int:
        """Return the active liquidity (uint128) at ``event_time``."""
        ...


@dataclass(frozen=True, slots=True)
class ConstantLiquidityModel:
    """The reference :class:`LiquidityModel`.

    The model returns a fixed ``active_liquidity`` for every query. It
    is the canonical "stable pool depth" reference; richer models can
    inherit the dataclass pattern and override the property / method.

    Field units:

    - ``active_liquidity_value`` — uint128 atomic liquidity units
      (the V4 wire format).
    """

    model_version: str = LIQUIDITY_MODEL_VERSION
    active_liquidity_value: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidModelVersionError(
                f"ConstantLiquidityModel.model_version: must be non-empty str, "
                f"got {self.model_version!r}"
            )
        if not isinstance(self.active_liquidity_value, int) or isinstance(
            self.active_liquidity_value, bool
        ):
            raise InvalidUnitError(
                f"ConstantLiquidityModel.active_liquidity_value: must be int, "
                f"got {type(self.active_liquidity_value).__name__}"
            )
        if self.active_liquidity_value < 0 or self.active_liquidity_value >= (1 << 128):
            raise InvalidUnitError(
                f"ConstantLiquidityModel.active_liquidity_value: must fit in "
                f"uint128, got {self.active_liquidity_value}"
            )

    def active_liquidity(self, *, pool_key_id: str, chain_id: int, event_time: int) -> int:
        """Return the constant liquidity for any query.

        ``pool_key_id`` / ``chain_id`` / ``event_time`` are accepted so a
        future variable-liquidity implementation can use them without
        breaking the call site; the constant implementation ignores them
        by design.
        """
        if not isinstance(pool_key_id, str) or not pool_key_id:
            raise ModelError(
                f"ConstantLiquidityModel.active_liquidity: pool_key_id must be "
                f"non-empty str, got {pool_key_id!r}"
            )
        if not isinstance(chain_id, int) or isinstance(chain_id, bool):
            raise ModelError(
                f"ConstantLiquidityModel.active_liquidity: chain_id must be int, "
                f"got {type(chain_id).__name__}"
            )
        return self.active_liquidity_value


# ---------------------------------------------------------------------------
# Fee model
# ---------------------------------------------------------------------------


#: Version of the reference :class:`FeeModel` implementation.
FEE_MODEL_VERSION: Final[str] = "t061.fee_model.v1"


@runtime_checkable
class FeeModel(Protocol):
    """The interface for the backtest fee model.

    A :class:`FeeModel` answers "what fee tier does the pool charge at
    time ``t``?" The engine uses the answer to compute realised fee
    revenue in Q64.64 (dimensionless ratio).
    """

    @property
    def model_version(self) -> str:
        """The version string of this model instance."""
        ...

    def fee_pips(self, *, pool_key_id: str, chain_id: int, event_time: int) -> int:
        """Return the fee tier in pips (0 ≤ value ≤ :data:`FEE_DENOMINATOR_PIPS`)."""
        ...


@dataclass(frozen=True, slots=True)
class StaticFeeModel:
    """The reference :class:`FeeModel`.

    The model returns a fixed ``fee_pips`` value. The unit is *pips*;
    ``FEE_DENOMINATOR_PIPS`` is "100 %".

    Field units:

    - ``fee_pips_value`` — integer pips in
      ``[0, FEE_DENOMINATOR_PIPS]``.
    """

    model_version: str = FEE_MODEL_VERSION
    fee_pips_value: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidModelVersionError(
                f"StaticFeeModel.model_version: must be non-empty str, got {self.model_version!r}"
            )
        if not isinstance(self.fee_pips_value, int) or isinstance(self.fee_pips_value, bool):
            raise InvalidUnitError(
                f"StaticFeeModel.fee_pips_value: must be int, got {type(self.fee_pips_value).__name__}"
            )
        if self.fee_pips_value < 0 or self.fee_pips_value > FEE_DENOMINATOR_PIPS:
            raise InvalidUnitError(
                f"StaticFeeModel.fee_pips_value: must be in [0, "
                f"{FEE_DENOMINATOR_PIPS}], got {self.fee_pips_value}"
            )

    def fee_pips(self, *, pool_key_id: str, chain_id: int, event_time: int) -> int:
        """Return the constant fee tier for any query."""
        if not isinstance(pool_key_id, str) or not pool_key_id:
            raise ModelError(
                f"StaticFeeModel.fee_pips: pool_key_id must be non-empty str, got {pool_key_id!r}"
            )
        return self.fee_pips_value


# ---------------------------------------------------------------------------
# Gas model
# ---------------------------------------------------------------------------


#: Version of the reference :class:`GasModel` implementation.
GAS_MODEL_VERSION: Final[str] = "t061.gas_model.v1"


@runtime_checkable
class GasModel(Protocol):
    """The interface for the backtest gas model.

    A :class:`GasModel` answers "how many native-gas units does this
    action consume?" The engine uses the answer to compute the
    action-level gas cost in raw gas units.
    """

    @property
    def model_version(self) -> str:
        """The version string of this model instance."""
        ...

    def gas_units(self, *, action_kind: str) -> int:
        """Return the gas cost of ``action_kind`` in atomic gas units."""
        ...


@dataclass(frozen=True, slots=True)
class FlatGasModel:
    """The reference :class:`GasModel`.

    The model returns a fixed gas cost for every action kind. A more
    elaborate model could differentiate between ``MINT`` / ``BURN`` /
    ``SWAP``; the reference model uses one value for every action.

    Field units:

    - ``gas_units_value`` — atomic gas units (integer).
    """

    model_version: str = GAS_MODEL_VERSION
    gas_units_value: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidModelVersionError(
                f"FlatGasModel.model_version: must be non-empty str, got {self.model_version!r}"
            )
        if not isinstance(self.gas_units_value, int) or isinstance(self.gas_units_value, bool):
            raise InvalidUnitError(
                f"FlatGasModel.gas_units_value: must be int, got {type(self.gas_units_value).__name__}"
            )
        if self.gas_units_value < 0 or self.gas_units_value >= (1 << 64):
            raise InvalidUnitError(
                f"FlatGasModel.gas_units_value: must fit in uint64, got {self.gas_units_value}"
            )

    def gas_units(self, *, action_kind: str) -> int:
        """Return the constant gas cost for any action kind."""
        if not isinstance(action_kind, str) or not action_kind:
            raise ModelError(
                f"FlatGasModel.gas_units: action_kind must be non-empty str, got {action_kind!r}"
            )
        return self.gas_units_value


# ---------------------------------------------------------------------------
# Slippage model
# ---------------------------------------------------------------------------


#: Version of the reference :class:`SlippageModel` implementation.
SLIPPAGE_MODEL_VERSION: Final[str] = "t061.slippage_model.v1"


@runtime_checkable
class SlippageModel(Protocol):
    """The interface for the backtest slippage model.

    A :class:`SlippageModel` answers "what is the price impact, in basis
    points, for a notional of ``size_q64_64`` at time ``t``?" The
    engine uses the answer to compute realised fill prices and to
    bound slippage arithmetic.

    The interface is intentionally narrow: the engine only asks for the
    price impact in bps; converting back to a fill price is the engine's
    job, because the fill price also depends on the trigger price and
    the trigger time.
    """

    @property
    def model_version(self) -> str:
        """The version string of this model instance."""
        ...

    def price_impact_bps(
        self, *, pool_key_id: str, chain_id: int, event_time: int, size_q64_64: int
    ) -> int:
        """Return the price impact in bps (≥ 0)."""
        ...


@dataclass(frozen=True, slots=True)
class ZeroSlippageModel:
    """The reference zero-impact :class:`SlippageModel`.

    The model returns ``0`` bps of price impact for every query. It is
    the canonical "perfect execution" reference; a richer model can
    inherit the dataclass pattern and override the method.

    Field units: ``None``; the model has no parameters beyond its
    version.
    """

    model_version: str = SLIPPAGE_MODEL_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidModelVersionError(
                f"ZeroSlippageModel.model_version: must be non-empty str, got {self.model_version!r}"
            )

    def price_impact_bps(
        self, *, pool_key_id: str, chain_id: int, event_time: int, size_q64_64: int
    ) -> int:
        """Return ``0`` bps of price impact for any query."""
        return 0


@dataclass(frozen=True, slots=True)
class ConstantSlippageModel:
    """A constant-impact :class:`SlippageModel`.

    The model returns a fixed ``impact_bps`` for every query. The unit
    is *basis points*; ``SLIPPAGE_DENOMINATOR_BPS`` is "100 %".

    Field units:

    - ``impact_bps`` — integer basis points in
      ``[0, SLIPPAGE_DENOMINATOR_BPS]``.
    """

    model_version: str = SLIPPAGE_MODEL_VERSION
    impact_bps: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidModelVersionError(
                f"ConstantSlippageModel.model_version: must be non-empty str, "
                f"got {self.model_version!r}"
            )
        if not isinstance(self.impact_bps, int) or isinstance(self.impact_bps, bool):
            raise InvalidUnitError(
                f"ConstantSlippageModel.impact_bps: must be int, got "
                f"{type(self.impact_bps).__name__}"
            )
        if self.impact_bps < 0 or self.impact_bps > SLIPPAGE_DENOMINATOR_BPS:
            raise InvalidUnitError(
                f"ConstantSlippageModel.impact_bps: must be in [0, "
                f"{SLIPPAGE_DENOMINATOR_BPS}], got {self.impact_bps}"
            )

    def price_impact_bps(
        self, *, pool_key_id: str, chain_id: int, event_time: int, size_q64_64: int
    ) -> int:
        """Return the constant impact for any query."""
        if not isinstance(pool_key_id, str) or not pool_key_id:
            raise ModelError(
                f"ConstantSlippageModel.price_impact_bps: pool_key_id must be "
                f"non-empty str, got {pool_key_id!r}"
            )
        return self.impact_bps


# ---------------------------------------------------------------------------
# Failure model
# ---------------------------------------------------------------------------


#: Version of the reference :class:`FailureModel` implementation.
FAILURE_MODEL_VERSION: Final[str] = "t061.failure_model.v1"


class FillOutcome(StrEnum):
    """The closed set of outcomes a :class:`FailureModel` may return.

    The engine maps the outcome to the audit-event status:

    - ``FILLED`` — the engine emits :attr:`STATUS_FILL_FILLED`.
    - ``PARTIAL`` — the engine emits :attr:`STATUS_FILL_PARTIAL` and
      records the *filled* liquidity; the unfilled remainder is
      recorded in the payload.
    - ``DELAYED`` — the engine emits :attr:`STATUS_FILL_DELAYED` and
      pushes the fill to the next visible data event; the fill price is
      the price at the new fill time, not the price that triggered the
      decision.
    - ``REJECTED`` — the engine emits :attr:`STATUS_FILL_REJECTED` and
      records the rejection; the ledger is unchanged.
    """

    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    DELAYED = "DELAYED"
    REJECTED = "REJECTED"


@runtime_checkable
class FailureModel(Protocol):
    """The interface for the backtest failure model.

    A :class:`FailureModel` answers "does this intent succeed, get
    rejected, fill partially, or get delayed?" The engine uses the
    answer to route the fill pipeline and to record the outcome on the
    audit-event payload.
    """

    @property
    def model_version(self) -> str:
        """The version string of this model instance."""
        ...

    def classify(
        self,
        *,
        pool_key_id: str,
        chain_id: int,
        event_time: int,
        requested_liquidity: int,
    ) -> FillOutcome:
        """Classify the outcome of an intent at ``event_time``."""
        ...


@dataclass(frozen=True, slots=True)
class DeterministicFailureModel:
    """The reference :class:`FailureModel`.

    The model classifies every intent as one of four outcomes according
    to a deterministic schedule. The schedule is a tuple of
    ``(event_time, outcome)`` pairs the model inspects in order; the
    first matching event time wins, ties broken by insertion order. If
    no entry matches, the model returns ``FILLED`` — the canonical
    "happy path" default.

    Field units: ``None``; the model carries no numeric parameters
    beyond the schedule.
    """

    model_version: str = FAILURE_MODEL_VERSION
    schedule: tuple[tuple[int, str], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.model_version, str) or not self.model_version:
            raise InvalidModelVersionError(
                f"DeterministicFailureModel.model_version: must be non-empty "
                f"str, got {self.model_version!r}"
            )
        if not isinstance(self.schedule, tuple):
            raise ModelError(
                f"DeterministicFailureModel.schedule: must be tuple[(int, str)], "
                f"got {type(self.schedule).__name__}"
            )
        for i, entry in enumerate(self.schedule):
            if not (isinstance(entry, tuple) and len(entry) == 2):
                raise ModelError(
                    f"DeterministicFailureModel.schedule[{i}]: must be (event_time, outcome)"
                )
            t, outcome = entry
            if not isinstance(t, int) or isinstance(t, bool):
                raise ModelError(
                    f"DeterministicFailureModel.schedule[{i}][0]: must be int, "
                    f"got {type(t).__name__}"
                )
            if t < 0:
                raise ModelError(
                    f"DeterministicFailureModel.schedule[{i}][0]: must be non-negative, got {t}"
                )
            if not isinstance(outcome, str):
                raise InvalidFillOutcomeError(
                    f"DeterministicFailureModel.schedule[{i}][1]: must be str, "
                    f"got {type(outcome).__name__}"
                )
            try:
                FillOutcome(outcome)
            except ValueError as e:
                raise InvalidFillOutcomeError(
                    f"DeterministicFailureModel.schedule[{i}][1]: {outcome!r} "
                    f"is not a valid FillOutcome"
                ) from e

    def classify(
        self,
        *,
        pool_key_id: str,
        chain_id: int,
        event_time: int,
        requested_liquidity: int,
    ) -> FillOutcome:
        """Return the first matching outcome for ``event_time``."""
        if not isinstance(pool_key_id, str) or not pool_key_id:
            raise ModelError(
                f"DeterministicFailureModel.classify: pool_key_id must be "
                f"non-empty str, got {pool_key_id!r}"
            )
        if not isinstance(event_time, int) or isinstance(event_time, bool):
            raise ModelError(
                f"DeterministicFailureModel.classify: event_time must be int, "
                f"got {type(event_time).__name__}"
            )
        if event_time < 0:
            raise ModelError(
                f"DeterministicFailureModel.classify: event_time must be "
                f"non-negative, got {event_time}"
            )
        if not isinstance(requested_liquidity, int) or isinstance(requested_liquidity, bool):
            raise ModelError(
                f"DeterministicFailureModel.classify: requested_liquidity must "
                f"be int, got {type(requested_liquidity).__name__}"
            )
        for t, outcome in self.schedule:
            if event_time == t:
                return FillOutcome(outcome)
        return FillOutcome.FILLED


# ---------------------------------------------------------------------------
# Composite model bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelBundle:
    """The composite bundle the engine consumes.

    The bundle pairs every model the pipeline needs with the parameter
    values it was constructed from. Two bundles with the same model
    versions and the same parameters produce byte-identical audit-event
    payloads; the bundle is the unit the engine serialises alongside
    the input manifest for a reproducible rerun.

    The bundle does **not** carry the input events themselves — those
    live in the manifest. The bundle is the *parameter* half of the
    experiment contract; the manifest is the *data* half.
    """

    bundle_version: str
    liquidity: LiquidityModel
    fee: FeeModel
    gas: GasModel
    slippage: SlippageModel
    failure: FailureModel
    latency_units: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.bundle_version, str) or not self.bundle_version:
            raise InvalidModelVersionError(
                f"ModelBundle.bundle_version: must be non-empty str, got {self.bundle_version!r}"
            )
        if not isinstance(self.liquidity, LiquidityModel):
            raise ModelError(
                f"ModelBundle.liquidity: must implement LiquidityModel, got "
                f"{type(self.liquidity).__name__}"
            )
        if not isinstance(self.fee, FeeModel):
            raise ModelError(
                f"ModelBundle.fee: must implement FeeModel, got {type(self.fee).__name__}"
            )
        if not isinstance(self.gas, GasModel):
            raise ModelError(
                f"ModelBundle.gas: must implement GasModel, got {type(self.gas).__name__}"
            )
        if not isinstance(self.slippage, SlippageModel):
            raise ModelError(
                f"ModelBundle.slippage: must implement SlippageModel, got "
                f"{type(self.slippage).__name__}"
            )
        if not isinstance(self.failure, FailureModel):
            raise ModelError(
                f"ModelBundle.failure: must implement FailureModel, got "
                f"{type(self.failure).__name__}"
            )
        if not isinstance(self.latency_units, int) or isinstance(self.latency_units, bool):
            raise InvalidUnitError(
                f"ModelBundle.latency_units: must be int, got {type(self.latency_units).__name__}"
            )
        if self.latency_units < 0 or self.latency_units >= (1 << 32):
            raise InvalidUnitError(
                f"ModelBundle.latency_units: must fit in uint32, got {self.latency_units}"
            )

    @property
    def bundle_hash(self) -> str:
        """Return a deterministic SHA-256 hex digest of the bundle."""
        import hashlib

        content = (
            f"{self.bundle_version}|{self.liquidity.model_version}|"
            f"{self.fee.model_version}|{self.gas.model_version}|"
            f"{self.slippage.model_version}|{self.failure.model_version}|"
            f"{self.latency_units}"
        )
        return "0x" + hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Layer-purity check
# ---------------------------------------------------------------------------


_FORBIDDEN_BACKTEST_MODELS_ROBINHOOD_MODULES: Final[tuple[str, ...]] = (
    "robinhood_lp.backtest.engine",
    "robinhood_lp.backtest.pipeline",
    "robinhood_lp.config",
    "robinhood_lp.discovery",
    "robinhood_lp.features",
    "robinhood_lp.ingestion",
    "robinhood_lp.presentation",
    "robinhood_lp.qualification",
    "robinhood_lp.quality",
    "robinhood_lp.replay",
    "robinhood_lp.rpc",
    "robinhood_lp.risk",
    "robinhood_lp.storage",
    "robinhood_lp.strategy",
    "robinhood_lp.execution",
)


def assert_backtest_models_layer_is_pure() -> None:
    """Raise :class:`ModelError` if this module imports a forbidden sibling.

    The denylist allows only :mod:`robinhood_lp.backtest.events`; any
    other ``robinhood_lp`` import is a contract break. The check walks
    the live module graph at the same granularity as the strategy-layer
    purity check.
    """
    module = importlib.import_module("robinhood_lp.backtest.models")
    forbidden: list[tuple[str, str]] = []
    for _attr_name, attr in vars(module).items():
        mod_name = getattr(attr, "__name__", None)
        if not isinstance(mod_name, str):
            continue
        if mod_name == "robinhood_lp.backtest.events" or mod_name.startswith(
            "robinhood_lp.backtest.events."
        ):
            continue
        if mod_name == "robinhood_lp.backtest.models" or mod_name.startswith(
            "robinhood_lp.backtest.models."
        ):
            continue
        for forbidden_root in _FORBIDDEN_BACKTEST_MODELS_ROBINHOOD_MODULES:
            if mod_name == forbidden_root or mod_name.startswith(forbidden_root + "."):
                forbidden.append(("robinhood_lp.backtest.models", mod_name))
                break
    if forbidden:
        rendered = "\n".join(f"  {src}: imports {imp}" for src, imp in forbidden)
        raise ModelError(f"backtest models module imports forbidden modules:\n{rendered}")


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "FEE_DENOMINATOR_PIPS",
    "FEE_MODEL_VERSION",
    "FAILURE_MODEL_VERSION",
    "FillOutcome",
    "FlatGasModel",
    "GAS_MODEL_VERSION",
    "GasModel",
    "InvalidFillOutcomeError",
    "InvalidModelVersionError",
    "InvalidUnitError",
    "LIQUIDITY_MODEL_VERSION",
    "LiquidityModel",
    "ModelBundle",
    "ModelError",
    "SLIPPAGE_DENOMINATOR_BPS",
    "SLIPPAGE_MODEL_VERSION",
    "SlippageModel",
    "StaticFeeModel",
    "ZeroSlippageModel",
    "ConstantLiquidityModel",
    "ConstantSlippageModel",
    "DeterministicFailureModel",
    "FeeModel",
    "FailureModel",
    "assert_backtest_models_layer_is_pure",
]
