"""Robustness scenario catalogue (T064).

DS-035 binds every robustness run to a catalogue the run reads
*before* it touches any data. Each catalogue entry declares its
unique outcome — either a ``HALT`` with a named reason code, or a
``DEGRADE`` to a deterministic, named fallback — and the catalogue
is the only place those outcomes are recorded. The catalogue is
appended to the run output and rejected at construction if any
entry is missing its declared outcome.

This module defines the catalogue primitive the T064 acceptance
clause binds:

- :class:`ScenarioOutcomeKind` — the closed vocabulary
  (``HALT`` / ``DEGRADE``).
- :class:`Scenario` — a single catalogue entry with its declared
  outcome and the named reason / fallback.
- :class:`ScenarioCatalogue` — the ordered, append-only catalogue
  the runner consults.
- :func:`default_stress_scenario_catalogue` — the canonical
  catalogue the runner uses when no custom catalogue is supplied,
  covering the stressed gas/latency/slippage/fee cases plus the
  missing-data and reorg cases T064 lists as deliverables.

DS-041 is enforced separately in
:mod:`robinhood_lp.robustness.degradation`: every ``DEGRADE``
fallback name must resolve to a registered, deterministic rule-
based fallback. ``DEGRADE`` never resolves to undefined behaviour
or a forced trade.

Design constraints:

- **Determinism.** The catalogue is a frozen tuple; two equivalent
  catalogues in any process produce byte-identical JSON.

- **Closed vocabulary.** The outcome kind, reason code, fallback
  name, and category are all closed vocabulary strings.

- **Layer purity.** The module imports only the standard library
  and the in-package :mod:`robinhood_lp.robustness.degradation`
  module. It does not import the backtest engine, the manifest
  layer, RPC, storage, signing, or presentation code.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from robinhood_lp.robustness.degradation import (
    VALID_FALLBACK_NAMES,
    DegradationError,
    DegradationPolicy,
)

#: Module version.
SCENARIOS_VERSION: Final[str] = "t064.robustness_scenarios.v1"

#: Closed vocabulary for the scenario outcome kind. ``HALT`` ends the
#: run with a named reason code; ``DEGRADE`` switches to a named
#: deterministic fallback (DS-035).
VALID_OUTCOME_KINDS: Final[frozenset[str]] = frozenset({"HALT", "DEGRADE"})

#: Closed vocabulary for the scenario category. The T064 deliverable
#: names six categories; additional categories can be added later
#: without breaking the contract.
VALID_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "STRESSED_GAS",
        "STRESSED_LATENCY",
        "STRESSED_SLIPPAGE",
        "STRESSED_FEE",
        "MISSING_DATA",
        "REORG",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ScenarioError(ValueError):
    """Base class for scenario catalogue construction / validation failures."""


class InvalidScenarioOutcomeError(ScenarioError):
    """A scenario carries an out-of-vocabulary outcome kind."""


class MissingScenarioOutcomeError(ScenarioError):
    """A HALT/DEGRADE scenario is missing its named reason / fallback.

    DS-035 binds this rejection: every catalogue entry must declare
    its unique outcome before the run starts; a scenario that names
    ``HALT`` without a reason code (or ``DEGRADE`` without a
    fallback name) is incomplete.
    """


class InvalidScenarioCategoryError(ScenarioError):
    """A scenario carries an out-of-vocabulary category."""


# ---------------------------------------------------------------------------
# Scenario and catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scenario:
    """A single entry in the robustness scenario catalogue.

    Each scenario declares one of two outcomes before the run starts
    (DS-035):

    - ``HALT`` — the run stops with the named ``reason_code``;
      ``fallback_name`` must be empty.
    - ``DEGRADE`` — the run switches to the named ``fallback_name``;
      ``reason_code`` must be empty.

    The category names which knob the scenario stresses; the
    severity parameter is the integer scaling the runner applies to
    the underlying model parameter (e.g. gas multiplier, latency
    addend, slippage bps addend, fee pips multiplier).

    Field units:

    - ``scenario_id`` — non-empty string; the catalogue's primary key.
    - ``category`` — one of :data:`VALID_CATEGORIES`.
    - ``description`` — non-empty human-readable string.
    - ``outcome_kind`` — one of :data:`VALID_OUTCOME_KINDS`.
    - ``reason_code`` — non-empty when ``outcome_kind == "HALT"``;
      empty otherwise.
    - ``fallback_name`` — non-empty when ``outcome_kind ==
      "DEGRADE"``; empty otherwise. The name must resolve in
      :data:`VALID_FALLBACK_NAMES`.
    - ``severity_multiplier_q64_64`` — Q64.64 dimensionless ratio;
      ``Q64_SCALE`` (1<<64) is the no-op multiplier. Addends use
      ``severity_addend`` instead.
    - ``severity_addend`` — non-negative integer addend in the
      underlying unit (gas units, latency units, bps, pips). Zero
      is the no-op.
    """

    scenario_id: str
    category: str
    description: str
    outcome_kind: str
    reason_code: str
    fallback_name: str
    severity_multiplier_q64_64: int
    severity_addend: int

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id:
            raise ScenarioError("Scenario.scenario_id: must be non-empty str")
        if self.category not in VALID_CATEGORIES:
            raise InvalidScenarioCategoryError(
                f"Scenario.category: must be one of {sorted(VALID_CATEGORIES)}, "
                f"got {self.category!r}"
            )
        if not isinstance(self.description, str) or not self.description:
            raise ScenarioError("Scenario.description: must be non-empty str")
        if self.outcome_kind not in VALID_OUTCOME_KINDS:
            raise InvalidScenarioOutcomeError(
                f"Scenario.outcome_kind: must be one of {sorted(VALID_OUTCOME_KINDS)}, "
                f"got {self.outcome_kind!r}"
            )
        if not isinstance(self.reason_code, str):
            raise ScenarioError("Scenario.reason_code: must be str")
        if not isinstance(self.fallback_name, str):
            raise ScenarioError("Scenario.fallback_name: must be str")
        if self.outcome_kind == "HALT":
            if not self.reason_code:
                raise MissingScenarioOutcomeError(
                    f"Scenario {self.scenario_id!r}: HALT must declare a reason_code"
                )
            if self.fallback_name:
                raise ScenarioError(
                    f"Scenario {self.scenario_id!r}: HALT must not declare a fallback_name"
                )
        else:  # DEGRADE
            if not self.fallback_name:
                raise MissingScenarioOutcomeError(
                    f"Scenario {self.scenario_id!r}: DEGRADE must declare a fallback_name"
                )
            if self.reason_code:
                raise ScenarioError(
                    f"Scenario {self.scenario_id!r}: DEGRADE must not declare a reason_code"
                )
            if self.fallback_name not in VALID_FALLBACK_NAMES:
                raise ScenarioError(
                    f"Scenario {self.scenario_id!r}: fallback_name={self.fallback_name!r} "
                    f"is not registered in the degradation policy; "
                    f"DS-041 forbids unregistered fallbacks"
                )
        if not isinstance(self.severity_multiplier_q64_64, int) or isinstance(
            self.severity_multiplier_q64_64, bool
        ):
            raise ScenarioError(
                f"Scenario.severity_multiplier_q64_64: must be int, got "
                f"{type(self.severity_multiplier_q64_64).__name__}"
            )
        if self.severity_multiplier_q64_64 < 0:
            raise ScenarioError(
                f"Scenario.severity_multiplier_q64_64: must be non-negative, got "
                f"{self.severity_multiplier_q64_64}"
            )
        if not isinstance(self.severity_addend, int) or isinstance(self.severity_addend, bool):
            raise ScenarioError(
                f"Scenario.severity_addend: must be int, got {type(self.severity_addend).__name__}"
            )
        if self.severity_addend < 0:
            raise ScenarioError(
                f"Scenario.severity_addend: must be non-negative, got {self.severity_addend}"
            )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly ``dict`` representation."""
        return {
            "scenario_id": self.scenario_id,
            "category": self.category,
            "description": self.description,
            "outcome_kind": self.outcome_kind,
            "reason_code": self.reason_code,
            "fallback_name": self.fallback_name,
            "severity_multiplier_q64_64": self.severity_multiplier_q64_64,
            "severity_addend": self.severity_addend,
        }


@dataclass(frozen=True, slots=True)
class ScenarioCatalogue:
    """The full catalogue the runner reads before the run starts.

    The catalogue is ordered; the runner consults each entry in
    declaration order. The catalogue is append-only at the call
    site: it is constructed once from a tuple of :class:`Scenario`
    and never mutated. A duplicated ``scenario_id`` is rejected at
    construction because the catalogue's primary key must be unique.

    The catalogue is paired with a :class:`DegradationPolicy` so the
    runner can resolve every ``DEGRADE`` fallback to its registered
    deterministic fallback. The pairing is enforced at construction:
    a cataloguethat names a fallback not registered in the policy is
    rejected (DS-041).
    """

    version: str
    scenarios: tuple[Scenario, ...]
    degradation_policy: DegradationPolicy

    def __post_init__(self) -> None:
        if self.version != SCENARIOS_VERSION:
            raise ScenarioError(
                f"ScenarioCatalogue.version: must be {SCENARIOS_VERSION!r}, got {self.version!r}"
            )
        ids = [s.scenario_id for s in self.scenarios]
        if len(set(ids)) != len(ids):
            seen: set[str] = set()
            for sid in ids:
                if sid in seen:
                    raise ScenarioError(f"ScenarioCatalogue: duplicate scenario_id={sid!r}")
                seen.add(sid)
        # Verify every DEGRADE fallback resolves in the policy.
        for scenario in self.scenarios:
            if scenario.outcome_kind == "DEGRADE":
                try:
                    self.degradation_policy.resolve(fallback_name=scenario.fallback_name)
                except DegradationError as exc:
                    raise ScenarioError(
                        f"ScenarioCatalogue: scenario {scenario.scenario_id!r} "
                        f"declares fallback_name={scenario.fallback_name!r} which "
                        f"does not resolve in the degradation policy: {exc}"
                    ) from exc

    def by_id(self, scenario_id: str) -> Scenario:
        """Return the scenario with the given ID or raise :class:`ScenarioError`."""
        for scenario in self.scenarios:
            if scenario.scenario_id == scenario_id:
                return scenario
        raise ScenarioError(f"ScenarioCatalogue.by_id: no scenario with id={scenario_id!r}")

    def by_category(self, category: str) -> tuple[Scenario, ...]:
        """Return the scenarios in the given category, in declaration order."""
        return tuple(s for s in self.scenarios if s.category == category)

    def halt_scenarios(self) -> tuple[Scenario, ...]:
        """Return every HALT scenario, in declaration order."""
        return tuple(s for s in self.scenarios if s.outcome_kind == "HALT")

    def degrade_scenarios(self) -> tuple[Scenario, ...]:
        """Return every DEGRADE scenario, in declaration order."""
        return tuple(s for s in self.scenarios if s.outcome_kind == "DEGRADE")

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly ``dict`` representation."""
        return {
            "version": self.version,
            "scenarios": [s.to_dict() for s in self.scenarios],
        }


def build_scenario_catalogue(
    *,
    scenarios: Iterable[Scenario],
    degradation_policy: DegradationPolicy,
) -> ScenarioCatalogue:
    """Build a :class:`ScenarioCatalogue` from an iterable of scenarios.

    The function is the canonical builder: it deduplicates scenario
    IDs, validates the outcomes against the closed vocabularies, and
    pairs the catalogue with the supplied :class:`DegradationPolicy`.
    The builder does not sort the scenarios — the caller controls
    the declaration order.
    """
    return ScenarioCatalogue(
        version=SCENARIOS_VERSION,
        scenarios=tuple(scenarios),
        degradation_policy=degradation_policy,
    )


# ---------------------------------------------------------------------------
# Default catalogue — covers the T064 stress / failure categories
# ---------------------------------------------------------------------------


#: Q64.64 multiplier scale (no-op).
_NOOP_MULTIPLIER_Q64_64: Final[int] = 1 << 64

#: Convenience: integer for "no addend".
_NO_ADDEND: Final[int] = 0


def default_stress_scenario_catalogue(
    *,
    degradation_policy: DegradationPolicy,
) -> ScenarioCatalogue:
    """Return the canonical T064 stress scenario catalogue.

    The catalogue covers the six categories the T064 deliverable
    names:

    - ``STRESSED_GAS`` — gas units scaled by 10×; ``HALT`` with
      ``GAS_BREACH`` because gas blow-up invalidates the cost
      assumption.
    - ``STRESSED_LATENCY`` — latency addend of 600 units; ``HALT``
      with ``LATENCY_VIOLATION`` because latency > the visibility
      window makes fills non-deterministic.
    - ``STRESSED_SLIPPAGE`` — slippage bps addend of 200 bps;
      ``HALT`` with ``SLIPPAGE_BREACH`` because the slippage
      assumption no longer reflects execution reality.
    - ``STRESSED_FEE`` — fee multiplier 0.5×; ``DEGRADE`` to
      ``USE_BROAD_RANGE_FALLBACK`` because fees halved but the
      Range strategy is no longer appropriate; the fallback is
      a deterministic rule-based baseline.
    - ``MISSING_DATA`` — no data for one quarter of the run;
      ``HALT`` with ``MISSING_DATA`` because no deterministic
      fallback exists for a totally missing observation window.
    - ``REORG`` — observed reorg depth > depth bound; ``HALT``
      with ``REORG_DETECTED`` because the audit chain cannot be
      reconciled.

    The cataloguethat is the T064 default. A runner that wants a
    custom catalogue (e.g. for a different severity) builds its own
    via :func:`build_scenario_catalogue`; the catalogue is the
    single source of declared outcomes, never the runner.
    """
    scenarios: tuple[Scenario, ...] = (
        Scenario(
            scenario_id="gas_10x_breach",
            category="STRESSED_GAS",
            description="Gas units multiplied by 10x to stress the cost assumption.",
            outcome_kind="HALT",
            reason_code="GAS_BREACH",
            fallback_name="",
            severity_multiplier_q64_64=10 * _NOOP_MULTIPLIER_Q64_64,
            severity_addend=_NO_ADDEND,
        ),
        Scenario(
            scenario_id="latency_plus_600_breach",
            category="STRESSED_LATENCY",
            description="Latency addend of 600 units (5 minutes) on top of the baseline.",
            outcome_kind="HALT",
            reason_code="LATENCY_VIOLATION",
            fallback_name="",
            severity_multiplier_q64_64=_NOOP_MULTIPLIER_Q64_64,
            severity_addend=600,
        ),
        Scenario(
            scenario_id="slippage_plus_200bps_breach",
            category="STRESSED_SLIPPAGE",
            description="Slippage addend of 200 basis points on top of the baseline.",
            outcome_kind="HALT",
            reason_code="SLIPPAGE_BREACH",
            fallback_name="",
            severity_multiplier_q64_64=_NOOP_MULTIPLIER_Q64_64,
            severity_addend=200,
        ),
        Scenario(
            scenario_id="fee_halved_degrade",
            category="STRESSED_FEE",
            description="Fee multiplier 0.5x; the Range strategy no longer matches fee revenue.",
            outcome_kind="DEGRADE",
            reason_code="",
            fallback_name="USE_BROAD_RANGE_FALLBACK",
            severity_multiplier_q64_64=_NOOP_MULTIPLIER_Q64_64 // 2,
            severity_addend=_NO_ADDEND,
        ),
        Scenario(
            scenario_id="missing_data_quarter_halt",
            category="MISSING_DATA",
            description="One quarter of the run has no data events; cannot continue.",
            outcome_kind="HALT",
            reason_code="MISSING_DATA",
            fallback_name="",
            severity_multiplier_q64_64=_NOOP_MULTIPLIER_Q64_64,
            severity_addend=_NO_ADDEND,
        ),
        Scenario(
            scenario_id="reorg_depth_exceeded_halt",
            category="REORG",
            description="Observed reorg depth exceeds the configured depth bound.",
            outcome_kind="HALT",
            reason_code="REORG_DETECTED",
            fallback_name="",
            severity_multiplier_q64_64=_NOOP_MULTIPLIER_Q64_64,
            severity_addend=_NO_ADDEND,
        ),
    )
    return build_scenario_catalogue(
        scenarios=scenarios,
        degradation_policy=degradation_policy,
    )


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def assert_catalogue_complete(catalogue: ScenarioCatalogue) -> None:
    """Raise :class:`ScenarioError` if any scenario is missing its declared outcome.

    The function is a defensive validation pass the runner calls
    *before* the run touches any data; it surfaces
    :class:`MissingScenarioOutcomeError` for any HALT without a
    reason code or any DEGRADE without a fallback name. The
    constructor of :class:`Scenario` already enforces these
    invariants; the function exists so a runner can re-check a
    catalogue that came from an external source.
    """
    for scenario in catalogue.scenarios:
        if scenario.outcome_kind == "HALT" and not scenario.reason_code:
            raise MissingScenarioOutcomeError(
                f"assert_catalogue_complete: HALT scenario "
                f"{scenario.scenario_id!r} has no reason_code"
            )
        if scenario.outcome_kind == "DEGRADE" and not scenario.fallback_name:
            raise MissingScenarioOutcomeError(
                f"assert_catalogue_complete: DEGRADE scenario "
                f"{scenario.scenario_id!r} has no fallback_name"
            )


def filter_by_categories(
    catalogue: ScenarioCatalogue,
    *,
    categories: Sequence[str],
) -> tuple[Scenario, ...]:
    """Return every scenario in ``categories`` (in declaration order).

    A category name not in :data:`VALID_CATEGORIES` is rejected at
    call time; the function does not silently drop bad names.
    """
    for category in categories:
        if category not in VALID_CATEGORIES:
            raise InvalidScenarioCategoryError(
                f"filter_by_categories: {category!r} is not a valid category"
            )
    return tuple(s for s in catalogue.scenarios if s.category in set(categories))


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "SCENARIOS_VERSION",
    "VALID_CATEGORIES",
    "VALID_OUTCOME_KINDS",
    "InvalidScenarioCategoryError",
    "InvalidScenarioOutcomeError",
    "MissingScenarioOutcomeError",
    "Scenario",
    "ScenarioCatalogue",
    "ScenarioError",
    "assert_catalogue_complete",
    "build_scenario_catalogue",
    "default_stress_scenario_catalogue",
    "filter_by_categories",
]
