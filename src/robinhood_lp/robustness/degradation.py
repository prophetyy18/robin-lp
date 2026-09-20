"""DS-041 degradation policy (T064).

DS-041 binds a strict fallback discipline: when a model output is
missing, outdated, unversioned, or outside the training distribution,
the system must *deterministically* fall back to a rules-based
baseline, not undefined behaviour or a forced trade. T064 binds
this rejection at the scenario-catalogue layer: every catalogue
entry that declares ``DEGRADE`` must name a fallback registered
with this module.

The module owns:

- :data:`VALID_FALLBACK_NAMES` — the closed vocabulary of
  registered fallback names.
- :class:`FallbackRule` — the deterministic rule a fallback
  resolves to.
- :class:`DegradationPolicy` — the registry / resolver the runner
  consults.
- :class:`DegradationError` — the error family the resolver raises
  on a missing fallback or a forbidden fallback ("no action",
  "force trade", etc.).

Design constraints:

- **No undefined behaviour.** A fallback that resolves to a
  ``NO_ACTION`` verdict without a registered rule is rejected at
  policy construction. The fallback must be a deterministic rule
  whose action the runner can carry out without consulting the
  model.

- **No forced trade.** A fallback that names a forced trade
  (e.g. ``FORCE_TRADE``) is rejected. DS-041 binds this.

- **Layer purity.** The module imports only the standard library.
  It does not import the backtest engine, the manifest layer,
  RPC, storage, signing, or presentation code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

#: Module version.
DEGRADATION_VERSION: Final[str] = "t064.robustness_degradation.v1"

#: Closed vocabulary of registered fallback names. The set is
#: exported so :mod:`robinhood_lp.robustness.scenarios` can validate
#: a scenario's ``fallback_name`` without importing the policy.
VALID_FALLBACK_NAMES: Final[frozenset[str]] = frozenset(
    {
        "USE_HODL_BASELINE",
        "USE_BROAD_RANGE_FALLBACK",
        "USE_FIXED_WIDTH_FALLBACK",
        "USE_HOLD_STRATEGY",
    }
)

#: Forbidden fallback names. A catalogue entry that names one of
#: these is rejected: ``NO_ACTION`` would be undefined behaviour
#: (no rule runs); ``FORCE_TRADE`` would violate DS-041.
FORBIDDEN_FALLBACK_NAMES: Final[frozenset[str]] = frozenset(
    {"NO_ACTION", "FORCE_TRADE", "UNDEFINED", "BEST_EFFORT"}
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DegradationError(ValueError):
    """Base class for degradation policy construction / resolution failures."""


class UnregisteredFallbackError(DegradationError):
    """A fallback name is not registered with the degradation policy.

    DS-041 binds this rejection: every ``DEGRADE`` scenario must
    name a fallback the policy can resolve to a deterministic rule.
    A name outside :data:`VALID_FALLBACK_NAMES` cannot be resolved
    and the resolver refuses the lookup.
    """


class ForbiddenFallbackError(DegradationError):
    """A fallback name is forbidden by DS-041.

    DS-041 forbids fallbacks that resolve to undefined behaviour or
    forced trades. The error names the offending name and the
    reason.
    """


# ---------------------------------------------------------------------------
# Fallback rule and policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FallbackRule:
    """A single deterministic rule a registered fallback resolves to.

    The rule carries a verb the runner can carry out without
    consulting the model:

    - ``HOLD_NO_TRADE`` — emit ``NO_TRADE`` for every decision;
      this is the canonical ``USE_HOLD_STRATEGY`` action.
    - ``OPEN_BROAD_RANGE`` — open the widest protocol-valid
      Range the pool's tick spacing admits; this is the
      canonical ``USE_BROAD_RANGE_FALLBACK`` action.
    - ``OPEN_FIXED_WIDTH`` — open a symmetric Range whose width
      is the registered ``width_ticks``; this is the canonical
      ``USE_FIXED_WIDTH_FALLBACK`` action.
    - ``BENCHMARK_ONLY`` — emit ``NO_TRADE`` and report the
      HODL benchmark only; this is the canonical
      ``USE_HODL_BASELINE`` action.

    Field units:

    - ``name`` — one of :data:`VALID_FALLBACK_NAMES`.
    - ``verb`` — closed vocabulary string above.
    - ``width_ticks`` — non-negative integer; the Range width the
      runner uses for ``OPEN_FIXED_WIDTH``. Zero is a sentinel
      meaning "fall back to the pool's tick-spacing-derived
      default".
    """

    name: str
    verb: str
    width_ticks: int

    def __post_init__(self) -> None:
        if self.name not in VALID_FALLBACK_NAMES:
            raise UnregisteredFallbackError(
                f"FallbackRule.name: must be one of {sorted(VALID_FALLBACK_NAMES)}, "
                f"got {self.name!r}"
            )
        valid_verbs = {"HOLD_NO_TRADE", "OPEN_BROAD_RANGE", "OPEN_FIXED_WIDTH", "BENCHMARK_ONLY"}
        if self.verb not in valid_verbs:
            raise DegradationError(
                f"FallbackRule.verb: must be one of {sorted(valid_verbs)}, got {self.verb!r}"
            )
        if not isinstance(self.width_ticks, int) or isinstance(self.width_ticks, bool):
            raise DegradationError(
                f"FallbackRule.width_ticks: must be int, got {type(self.width_ticks).__name__}"
            )
        if self.width_ticks < 0:
            raise DegradationError(
                f"FallbackRule.width_ticks: must be non-negative, got {self.width_ticks}"
            )


@dataclass(frozen=True, slots=True)
class DegradationPolicy:
    """The registry / resolver the runner consults on every ``DEGRADE``.

    The policy owns a frozen mapping from fallback name to
    :class:`FallbackRule`. The :meth:`resolve` method returns the
    rule for a given fallback name; an unknown name raises
    :class:`UnregisteredFallbackError`. The constructor rejects
    any entry in :data:`FORBIDDEN_FALLBACK_NAMES`.

    The policy is intentionally small: the runner is free to
    consult additional sources at runtime, but the *fallback name
    itself* must always be a registered rule. DS-041 binds this
    discipline.
    """

    version: str
    rules: tuple[FallbackRule, ...]

    def __post_init__(self) -> None:
        if self.version != DEGRADATION_VERSION:
            raise DegradationError(
                f"DegradationPolicy.version: must be {DEGRADATION_VERSION!r}, got {self.version!r}"
            )
        seen: set[str] = set()
        for rule in self.rules:
            if rule.name in FORBIDDEN_FALLBACK_NAMES:
                raise ForbiddenFallbackError(
                    f"DegradationPolicy: fallback {rule.name!r} is forbidden by "
                    f"DS-041 (resolves to undefined behaviour or a forced trade)"
                )
            if rule.name in seen:
                raise DegradationError(f"DegradationPolicy: duplicate fallback name={rule.name!r}")
            seen.add(rule.name)

    def resolve(self, *, fallback_name: str) -> FallbackRule:
        """Return the :class:`FallbackRule` for ``fallback_name``.

        The function is the single point every ``DEGRADE`` scenario
        routes through. An unknown name raises
        :class:`UnregisteredFallbackError`; a forbidden name (one
        of :data:`FORBIDDEN_FALLBACK_NAMES`) raises
        :class:`ForbiddenFallbackError`.
        """
        if not isinstance(fallback_name, str) or not fallback_name:
            raise DegradationError("DegradationPolicy.resolve: fallback_name must be non-empty str")
        if fallback_name in FORBIDDEN_FALLBACK_NAMES:
            raise ForbiddenFallbackError(
                f"DegradationPolicy.resolve: fallback_name={fallback_name!r} is forbidden by DS-041"
            )
        for rule in self.rules:
            if rule.name == fallback_name:
                return rule
        raise UnregisteredFallbackError(
            f"DegradationPolicy.resolve: fallback_name={fallback_name!r} "
            f"is not registered; the runner cannot resolve a DEGRADE "
            f"to undefined behaviour"
        )

    def has(self, fallback_name: str) -> bool:
        """Return ``True`` if the policy can resolve ``fallback_name``."""
        try:
            self.resolve(fallback_name=fallback_name)
        except DegradationError:
            return False
        return True


def build_degradation_policy(
    *,
    rules: Iterable[FallbackRule] = (),
) -> DegradationPolicy:
    """Build the canonical :class:`DegradationPolicy`.

    The function is the canonical builder. The caller passes the
    rules to register; the policy rejects duplicates and forbidden
    names at construction. The default empty rules tuple yields a
    policy that knows no fallback names — the runner must register
    every fallback it expects to use.
    """
    return DegradationPolicy(version=DEGRADATION_VERSION, rules=tuple(rules))


def default_degradation_policy() -> DegradationPolicy:
    """Return the canonical T064 degradation policy.

    The policy registers one rule per
    :data:`VALID_FALLBACK_NAMES` entry. Each rule resolves to a
    deterministic verb the runner can carry out without consulting
    the model:

    - ``USE_HODL_BASELINE`` — ``BENCHMARK_ONLY``.
    - ``USE_BROAD_RANGE_FALLBACK`` — ``OPEN_BROAD_RANGE``.
    - ``USE_FIXED_WIDTH_FALLBACK`` — ``OPEN_FIXED_WIDTH`` (default
      width 0 = pool-tick-spacing default).
    - ``USE_HOLD_STRATEGY`` — ``HOLD_NO_TRADE``.
    """
    rules: tuple[FallbackRule, ...] = (
        FallbackRule(name="USE_HODL_BASELINE", verb="BENCHMARK_ONLY", width_ticks=0),
        FallbackRule(name="USE_BROAD_RANGE_FALLBACK", verb="OPEN_BROAD_RANGE", width_ticks=0),
        FallbackRule(name="USE_FIXED_WIDTH_FALLBACK", verb="OPEN_FIXED_WIDTH", width_ticks=0),
        FallbackRule(name="USE_HOLD_STRATEGY", verb="HOLD_NO_TRADE", width_ticks=0),
    )
    return build_degradation_policy(rules=rules)


def assert_no_degrade_falls_through(
    *,
    catalogue_scenarios: Iterable[Mapping[str, str]],
    policy: DegradationPolicy,
) -> None:
    """Defensive: every DEGRADE scenario's fallback must resolve.

    The function is the DS-041 gate the runner calls before it
    touches any data. For every ``DEGRADE`` scenario in
    ``catalogue_scenarios`` (each entry must expose ``outcome_kind``
    and ``fallback_name`` keys), the function calls
    :meth:`DegradationPolicy.resolve` and raises if the lookup
    fails. A scenario that declares ``DEGRADE`` without a fallback
    name is rejected.
    """
    for entry in catalogue_scenarios:
        kind = entry.get("outcome_kind")
        if kind != "DEGRADE":
            continue
        fallback_name = entry.get("fallback_name", "")
        if not fallback_name:
            raise DegradationError(
                "assert_no_degrade_falls_through: DEGRADE scenario has no fallback_name"
            )
        policy.resolve(fallback_name=fallback_name)


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


__all__ = [
    "DEGRADATION_VERSION",
    "FORBIDDEN_FALLBACK_NAMES",
    "VALID_FALLBACK_NAMES",
    "DegradationError",
    "DegradationPolicy",
    "FallbackRule",
    "ForbiddenFallbackError",
    "UnregisteredFallbackError",
    "assert_no_degrade_falls_through",
    "build_degradation_policy",
    "default_degradation_policy",
]
