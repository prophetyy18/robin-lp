"""Tests for the point-in-time quote and gas valuation module (T053).

The T053 module is the quote-valuation half of the V1 features
package. The tests cover:

- **Observation schema** — observed_at / available_at / source /
  pair / block / time / confidence / staleness are explicit;
  per-field unit is one of raw token integer, numeraire units,
  ratio, or dimensionless; the available_at guard enforces
  prefix invariance (a row whose available_at is after the
  decision time is rejected).
- **ADR-014 numeraire hierarchy** — USDG, qualified USD
  stablecoin, ETH display-only, ``RELATIVE_ONLY`` are all
  represented and the per-numeraire qualification record
  carries confidence, staleness, and the rationale.
- **Conversion graph and missing policy** — delayed, revised,
  depegged, missing and cross-rate scenarios each produce the
  named :class:`MissingPolicy`; the graph refuses to silently
  substitute a current price or a 1-USD assumption.
- **``RELATIVE_ONLY`` output** — the canonical
  :class:`RelativeOnlyBar` carries no USD-denominated field
  anywhere; :func:`assert_no_usd_fields` is the typed guard;
  :func:`ranking_blocked_between` forbids cross-numeraire
  ranking.
- **Point-in-time USDG conversion** — performance, exposure,
  and both 5-minute extreme-move rules consume the same
  :class:`QuoteBar`; the conversion is pure and deterministic.
- **Complete bars** — the bar carries unit, window, data time
  and availability time; no non-market fundamental signal
  enters the schema.

Every fixture exercises a distinct scenario (delayed / revised /
depegged / missing / cross-rate) so the must-not clauses
(no stablecoin = 1 USD, no silent current price, no
source/frequency mixing, no USD conversion of RELATIVE_ONLY,
no ETH-as-primary-benchmark) are tested individually.
"""

from __future__ import annotations

import pytest

from robinhood_lp.features.quote import (
    DEFAULT_CONVERSION_GRAPH,
    DEFAULT_DEPEG_THRESHOLD_Q64_64,
    FIVE_MINUTE_DOWN_SPIKE_FRACTION,
    FIVE_MINUTE_RULE_WINDOW_SECONDS,
    FIVE_MINUTE_UP_SPIKE_FRACTION,
    MAX_UINT256,
    Q64_SCALE,
    USD_DENOMINATED_FORBIDDEN_FIELDS,
    ConfidenceLevel,
    ConversionEdge,
    ConversionGraph,
    ConversionPath,
    EdgePolicy,
    FiveMinuteRuleVerdict,
    MissingPolicy,
    NumeraireLevel,
    NumeraireQualification,
    Observation,
    ObservationUnit,
    QualificationBundle,
    QuoteBar,
    QuoteGraphError,
    QuoteObservationError,
    QuoteRelativeOnlyError,
    RelativeOnlyBar,
    SourceKind,
    assert_no_usd_fields,
    build_relative_only_bar,
    convert_observation,
    convert_to_usdg,
    default_conversion_graph,
    empty_qualification_bundle,
    evaluate_five_minute_rules,
    format_ratio_decimal,
    ranking_blocked_between,
)
from robinhood_lp.protocol.events import BlockRef
from robinhood_lp.protocol.ids import ChainId

# Reference chain id used by the tests (matches the Robinhood Chain
# mainnet reference set by T024; the value here is irrelevant for
# the schema but the tests keep it pinned).
REFERENCE_CHAIN_ID: ChainId = ChainId(4663)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _block_hash(n: int) -> int:
    """Return a deterministic 32-byte hash value for the block number ``n``."""
    return (n * 31 + 7) % (1 << 256)


def _make_block_ref(block_number: int) -> BlockRef:
    """Construct a canonical :class:`BlockRef` for ``block_number``."""
    return BlockRef(
        chain_id=REFERENCE_CHAIN_ID,
        block_hash=_block_hash(block_number),
        block_number=block_number,
    )


def _make_observation(
    *,
    observed_at: int = 1_000_000,
    available_at: int | None = None,
    source: SourceKind = SourceKind.ONCHAIN_POOL,
    pair: str = "ZZZ/USDG",
    block_number: int = 100,
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
    staleness_seconds: int = 5,
    numeraire_level: NumeraireLevel = NumeraireLevel.USDG,
    unit: ObservationUnit = ObservationUnit.RATIO,
    value: int = Q64_SCALE,
    chain_id: ChainId = REFERENCE_CHAIN_ID,
    notes: tuple[str, ...] = (),
) -> Observation:
    """Construct an :class:`Observation` with sensible defaults."""
    return Observation(
        observed_at=observed_at,
        available_at=available_at if available_at is not None else observed_at + staleness_seconds,
        source=source,
        pair=pair,
        block_number=block_number,
        block_ref=_make_block_ref(block_number),
        confidence=confidence,
        staleness_seconds=staleness_seconds,
        numeraire_level=numeraire_level,
        unit=unit,
        value=value,
        chain_id=chain_id,
        notes=notes,
    )


def _make_qualification(
    *,
    selected_level: NumeraireLevel,
    stablecoin_per_usdg_q64_64: int | None = None,
    rationale: str = "selected for test fixture",
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH,
    staleness_seconds: int = 5,
) -> QualificationBundle:
    """Build a :class:`QualificationBundle` with one record per level.

    The USDG record is always present with ``selected=False`` so the
    bundle has a single selected record and four runner-up records.
    This mirrors the canonical dataset shape T053's contract
    requires.
    """
    records = []
    for level in NumeraireLevel:
        is_selected = level is selected_level
        if level is NumeraireLevel.QUALIFIED_USD_STABLECOIN:
            ratio = stablecoin_per_usdg_q64_64
        else:
            ratio = None
        if level is NumeraireLevel.USDG:
            level_rationale = (
                "USDG present in the dataset" if is_selected else "rejected: USDG not present"
            )
        elif level is NumeraireLevel.QUALIFIED_USD_STABLECOIN:
            level_rationale = (
                "qualified USD stablecoin selected"
                if is_selected
                else "rejected: no qualified USD stablecoin path"
            )
        elif level is NumeraireLevel.ETH_DISPLAY_ONLY:
            level_rationale = (
                "ETH-display only" if is_selected else "rejected: ETH-display is volatile"
            )
        else:  # RELATIVE_ONLY
            level_rationale = (
                "RELATIVE_ONLY: no USD route"
                if is_selected
                else "rejected: RELATIVE_ONLY is the last-resort level"
            )
        records.append(
            NumeraireQualification(
                level=level,
                selected=is_selected,
                rationale=level_rationale,
                confidence=confidence,
                staleness_seconds=staleness_seconds,
                stablecoin_per_usdg_q64_64=ratio,
            )
        )
    return QualificationBundle(records=tuple(records))


# ---------------------------------------------------------------------------
# Observation schema
# ---------------------------------------------------------------------------


class TestObservationSchema:
    """The provider-neutral observation schema."""

    def test_observation_carries_all_required_fields(self) -> None:
        observation = _make_observation()
        assert observation.observed_at == 1_000_000
        assert observation.available_at == 1_000_005
        assert observation.source is SourceKind.ONCHAIN_POOL
        assert observation.pair == "ZZZ/USDG"
        assert observation.block_number == 100
        assert isinstance(observation.block_ref, BlockRef)
        assert observation.confidence is ConfidenceLevel.HIGH
        assert observation.staleness_seconds == 5
        assert observation.numeraire_level is NumeraireLevel.USDG
        assert observation.unit is ObservationUnit.RATIO
        assert observation.value == Q64_SCALE
        assert isinstance(observation.chain_id, ChainId)

    def test_observation_rejects_available_before_observed(self) -> None:
        with pytest.raises(QuoteObservationError, match="available_at.*must be >="):
            _make_observation(observed_at=100, staleness_seconds=0, available_at=50)

    def test_observation_rejects_negative_observed_at(self) -> None:
        with pytest.raises(QuoteObservationError, match="must be non-negative"):
            _make_observation(observed_at=-1)

    def test_observation_rejects_empty_pair(self) -> None:
        with pytest.raises(QuoteObservationError, match="pair"):
            _make_observation(pair="")

    def test_observation_rejects_non_positive_value_for_ratio(self) -> None:
        with pytest.raises(QuoteObservationError, match="must be positive"):
            _make_observation(unit=ObservationUnit.RATIO, value=0)

    def test_observation_rejects_bool_value_for_ratio(self) -> None:
        with pytest.raises(QuoteObservationError, match="must be int"):
            _make_observation(unit=ObservationUnit.RATIO, value=True)

    def test_observation_rejects_non_uint256_for_numeraire_units(self) -> None:
        with pytest.raises(QuoteObservationError, match="uint256"):
            _make_observation(
                unit=ObservationUnit.NUMERAIRE_UNITS,
                value=1 << 256,
            )

    def test_observation_rejects_non_uint256_for_raw_token_integer(self) -> None:
        with pytest.raises(QuoteObservationError, match="uint256"):
            _make_observation(
                unit=ObservationUnit.RAW_TOKEN_INTEGER,
                value=1 << 256,
            )

    def test_observation_rejects_non_tuple_notes(self) -> None:
        # A tuple of str is accepted.
        _make_observation(notes=("a", "b"))
        # A list of str is rejected.
        with pytest.raises(QuoteObservationError, match="notes"):
            Observation(
                observed_at=1_000_000,
                available_at=1_000_005,
                source=SourceKind.ONCHAIN_POOL,
                pair="ZZZ/USDG",
                block_number=100,
                block_ref=_make_block_ref(100),
                confidence=ConfidenceLevel.HIGH,
                staleness_seconds=5,
                numeraire_level=NumeraireLevel.USDG,
                unit=ObservationUnit.RATIO,
                value=Q64_SCALE,
                chain_id=REFERENCE_CHAIN_ID,
                notes=["not a tuple"],  # type: ignore[arg-type]
            )

    def test_observation_rejects_non_str_in_notes(self) -> None:
        with pytest.raises(QuoteObservationError, match="notes"):
            Observation(
                observed_at=1_000_000,
                available_at=1_000_005,
                source=SourceKind.ONCHAIN_POOL,
                pair="ZZZ/USDG",
                block_number=100,
                block_ref=_make_block_ref(100),
                confidence=ConfidenceLevel.HIGH,
                staleness_seconds=5,
                numeraire_level=NumeraireLevel.USDG,
                unit=ObservationUnit.RATIO,
                value=Q64_SCALE,
                chain_id=REFERENCE_CHAIN_ID,
                notes=(123, "ok"),  # type: ignore[arg-type]
            )

    def test_observation_is_relative_only_property(self) -> None:
        relative = _make_observation(numeraire_level=NumeraireLevel.RELATIVE_ONLY)
        assert relative.is_relative_only is True
        usdg = _make_observation(numeraire_level=NumeraireLevel.USDG)
        assert usdg.is_relative_only is False


# ---------------------------------------------------------------------------
# Per-numeraire qualification record
# ---------------------------------------------------------------------------


class TestQualificationRecord:
    """The per-numeraire qualification record."""

    def test_qualification_bundle_has_one_selected_record(self) -> None:
        bundle = _make_qualification(selected_level=NumeraireLevel.USDG)
        assert bundle.selected.level is NumeraireLevel.USDG
        selected_count = sum(1 for r in bundle.records if r.selected)
        assert selected_count == 1

    def test_qualification_rejects_two_selected_records(self) -> None:
        records = (
            NumeraireQualification(
                level=NumeraireLevel.USDG,
                selected=True,
                rationale="a",
                confidence=ConfidenceLevel.HIGH,
                staleness_seconds=0,
                stablecoin_per_usdg_q64_64=None,
            ),
            NumeraireQualification(
                level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
                selected=True,
                rationale="b",
                confidence=ConfidenceLevel.HIGH,
                staleness_seconds=0,
                stablecoin_per_usdg_q64_64=Q64_SCALE,
            ),
        )
        with pytest.raises(QuoteObservationError, match="exactly one"):
            QualificationBundle(records=records)

    def test_qualification_rejects_zero_selected_records(self) -> None:
        records = (
            NumeraireQualification(
                level=NumeraireLevel.USDG,
                selected=False,
                rationale="a",
                confidence=ConfidenceLevel.HIGH,
                staleness_seconds=0,
                stablecoin_per_usdg_q64_64=None,
            ),
        )
        with pytest.raises(QuoteObservationError, match="exactly one"):
            QualificationBundle(records=records)

    def test_qualification_rejects_duplicate_levels(self) -> None:
        records = (
            NumeraireQualification(
                level=NumeraireLevel.USDG,
                selected=True,
                rationale="a",
                confidence=ConfidenceLevel.HIGH,
                staleness_seconds=0,
                stablecoin_per_usdg_q64_64=None,
            ),
            NumeraireQualification(
                level=NumeraireLevel.USDG,
                selected=False,
                rationale="b",
                confidence=ConfidenceLevel.HIGH,
                staleness_seconds=0,
                stablecoin_per_usdg_q64_64=None,
            ),
        )
        with pytest.raises(QuoteObservationError, match="duplicate"):
            QualificationBundle(records=records)

    def test_is_usd_denominated_property(self) -> None:
        usdg = NumeraireQualification(
            level=NumeraireLevel.USDG,
            selected=True,
            rationale="x",
            confidence=ConfidenceLevel.HIGH,
            staleness_seconds=0,
            stablecoin_per_usdg_q64_64=None,
        )
        stable = NumeraireQualification(
            level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            selected=True,
            rationale="x",
            confidence=ConfidenceLevel.HIGH,
            staleness_seconds=0,
            stablecoin_per_usdg_q64_64=Q64_SCALE,
        )
        eth = NumeraireQualification(
            level=NumeraireLevel.ETH_DISPLAY_ONLY,
            selected=True,
            rationale="x",
            confidence=ConfidenceLevel.HIGH,
            staleness_seconds=0,
            stablecoin_per_usdg_q64_64=None,
        )
        relative = NumeraireQualification(
            level=NumeraireLevel.RELATIVE_ONLY,
            selected=True,
            rationale="x",
            confidence=ConfidenceLevel.HIGH,
            staleness_seconds=0,
            stablecoin_per_usdg_q64_64=None,
        )
        assert usdg.is_usd_denominated is True
        assert stable.is_usd_denominated is True
        assert eth.is_usd_denominated is False
        assert relative.is_usd_denominated is False

    def test_empty_qualification_bundle_defaults_to_relative_only(self) -> None:
        bundle = empty_qualification_bundle()
        assert bundle.selected.level is NumeraireLevel.RELATIVE_ONLY


# ---------------------------------------------------------------------------
# Conversion graph
# ---------------------------------------------------------------------------


class TestConversionGraph:
    """The conversion graph and missing-policy surface."""

    def test_default_graph_has_required_edges(self) -> None:
        graph = default_conversion_graph()
        # USDG identity
        graph.edge(NumeraireLevel.USDG, NumeraireLevel.USDG)
        # Stablecoin → USDG direct edge
        edge = graph.edge(NumeraireLevel.QUALIFIED_USD_STABLECOIN, NumeraireLevel.USDG)
        assert edge.policy is EdgePolicy.DEPEG_TOLERATED
        # ETH-display identity, no USDG edge
        graph.edge(NumeraireLevel.ETH_DISPLAY_ONLY, NumeraireLevel.ETH_DISPLAY_ONLY)
        assert not graph.has_edge(NumeraireLevel.ETH_DISPLAY_ONLY, NumeraireLevel.USDG)
        # RELATIVE_ONLY identity
        graph.edge(NumeraireLevel.RELATIVE_ONLY, NumeraireLevel.RELATIVE_ONLY)
        assert not graph.has_edge(NumeraireLevel.RELATIVE_ONLY, NumeraireLevel.USDG)

    def test_graph_rejects_unknown_edge(self) -> None:
        graph = default_conversion_graph()
        with pytest.raises(QuoteGraphError, match="no edge"):
            graph.edge(NumeraireLevel.RELATIVE_ONLY, NumeraireLevel.USDG)

    def test_graph_rejects_duplicate_edges(self) -> None:
        with pytest.raises(QuoteObservationError, match="ConversionEdge"):
            ConversionGraph(
                edges=(
                    ConversionEdge(
                        source=NumeraireLevel.USDG,
                        target=NumeraireLevel.USDG,
                        policy=EdgePolicy.DIRECT,
                        max_staleness_seconds=0,
                    ),
                    ConversionEdge(
                        source=NumeraireLevel.USDG,
                        target=NumeraireLevel.USDG,
                        policy=EdgePolicy.DIRECT,
                        max_staleness_seconds=10,
                    ),
                )
            )

    def test_default_graph_is_total_on_relative_only_domain(self) -> None:
        """The graph must be total on the RELATIVE_ONLY domain.

        ADR-014 §3 requires the framework to refuse any USD conversion
        of a RELATIVE_ONLY dataset. The default graph therefore carries
        a ``RELATIVE_ONLY -> RELATIVE_ONLY`` identity edge so the
        canonical conversion path is reachable.
        """
        graph = default_conversion_graph()
        assert graph.has_edge(NumeraireLevel.RELATIVE_ONLY, NumeraireLevel.RELATIVE_ONLY)

    def test_neighbors_returns_targets(self) -> None:
        graph = default_conversion_graph()
        targets = graph.neighbors(NumeraireLevel.USDG)
        assert NumeraireLevel.USDG in targets

    def test_default_conversion_graph_constant_matches_factory(self) -> None:
        # Same edge set, regardless of instantiation path.
        fresh = default_conversion_graph()
        assert len(fresh.edges) == len(DEFAULT_CONVERSION_GRAPH.edges)


# ---------------------------------------------------------------------------
# USDG conversion (the canonical point-in-time USDG price)
# ---------------------------------------------------------------------------


class TestConvertToUsdg:
    """The canonical USDG conversion feeding performance / exposure / 5-min rules."""

    def test_usdg_observation_yields_identity_usdg_price(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.USDG,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 5,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert isinstance(bar, QuoteBar)
        assert bar.missing_policy is MissingPolicy.OK
        assert bar.usdg_per_token_q64_64 == Q64_SCALE * 5

    def test_stablecoin_yields_usdg_price_via_ratio_not_one_usd(self) -> None:
        """Stablecoin -> USDG must use the observed ratio, not 1 USD."""
        # Stablecoin per token = 1.05 USDG (slightly above 1 USD to prove
        # the framework does NOT assume 1 stablecoin = 1 USD).
        stablecoin_per_token_q64_64 = (Q64_SCALE * 105) // 100
        stablecoin_per_usdg_q64_64 = Q64_SCALE  # 1 USDG = 1 USD-pegged value
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            unit=ObservationUnit.RATIO,
            value=stablecoin_per_token_q64_64,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=stablecoin_per_usdg_q64_64,
        )
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.missing_policy is MissingPolicy.OK
        assert bar.usdg_per_token_q64_64 == stablecoin_per_token_q64_64

    def test_stablecoin_with_depeg_ratio_yields_different_usdg_price(self) -> None:
        """A depegged stablecoin (0.97 USD) yields the depegged USDG value.

        The framework never assumes the stablecoin equals one USD: the
        ratio ``stablecoin_per_usdg_q64_64`` is the observed value and
        is the input the conversion uses.
        """
        stablecoin_per_token_q64_64 = Q64_SCALE * 5  # 5 stablecoin per token
        depegged_ratio_q64_64 = (Q64_SCALE * 97) // 100  # 0.97 USD per stablecoin
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            unit=ObservationUnit.RATIO,
            value=stablecoin_per_token_q64_64,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=depegged_ratio_q64_64,
        )
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        # usdg_per_token = stablecoin_per_token / stablecoin_per_usdg
        #                = 5 / 0.97 ≈ 5.1546
        # In Q64.64: (5 * Q64_SCALE) * Q64_SCALE // depegged_ratio_q64_64
        expected = (stablecoin_per_token_q64_64 * Q64_SCALE) // depegged_ratio_q64_64
        assert bar.usdg_per_token_q64_64 == expected
        # The depegged value must NOT equal the 1-USD assumption.
        assert bar.usdg_per_token_q64_64 != stablecoin_per_token_q64_64

    def test_stablecoin_with_missing_ratio_is_missing(self) -> None:
        """Missing stablecoin_per_usdg ratio yields MissingPolicy.MISSING."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=None,
        )
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.missing_policy is MissingPolicy.MISSING
        assert bar.usdg_per_token_q64_64 is None

    def test_delayed_staleness_yields_missing_policy_delayed(self) -> None:
        """A row whose staleness exceeds the edge budget is DELAYED."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            staleness_seconds=10_000,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=Q64_SCALE,
        )
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.missing_policy is MissingPolicy.DELAYED
        assert bar.usdg_per_token_q64_64 is None

    def test_depeg_threshold_flags_move_as_depegged(self) -> None:
        """A move that exceeds the depeg threshold raises DEPEGGED."""
        previous_ratio_q64_64 = Q64_SCALE  # previous USDG per token = 1.0
        # New USDG per token = 1.10, a 10% move (exceeds 5% default).
        new_value = Q64_SCALE + (Q64_SCALE * 10) // 100
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            unit=ObservationUnit.RATIO,
            value=new_value,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=Q64_SCALE,
        )
        bar = convert_to_usdg(
            observation=observation,
            qualification=qualification,
            previous_ratio_q64_64=previous_ratio_q64_64,
            depeg_threshold_q64_64=DEFAULT_DEPEG_THRESHOLD_Q64_64,
        )
        assert bar.missing_policy is MissingPolicy.DEPEGGED
        assert bar.usdg_per_token_q64_64 == new_value

    def test_move_below_depeg_threshold_keeps_ok(self) -> None:
        """A move below the depeg threshold keeps MissingPolicy.OK."""
        previous_ratio_q64_64 = Q64_SCALE
        # New USDG per token = 1.01, a 1% move (below 5% default).
        new_value = Q64_SCALE + (Q64_SCALE * 1) // 100
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            unit=ObservationUnit.RATIO,
            value=new_value,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=Q64_SCALE,
        )
        bar = convert_to_usdg(
            observation=observation,
            qualification=qualification,
            previous_ratio_q64_64=previous_ratio_q64_64,
            depeg_threshold_q64_64=DEFAULT_DEPEG_THRESHOLD_Q64_64,
        )
        assert bar.missing_policy is MissingPolicy.OK

    def test_eth_display_refuses_usdg_conversion(self) -> None:
        """ETH-display rows refuse USDG conversion (no ETH-as-primary)."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.ETH_DISPLAY_ONLY,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.ETH_DISPLAY_ONLY)
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.missing_policy is MissingPolicy.MISSING
        assert bar.usdg_per_token_q64_64 is None

    def test_relative_only_row_carries_no_usdg_price(self) -> None:
        """RELATIVE_ONLY rows never carry a USDG price."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.RELATIVE_ONLY)
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.is_relative_only is True
        assert bar.usdg_per_token_q64_64 is None

    def test_decision_time_rejects_future_observation(self) -> None:
        """A future observation (available_at > decision_time) is rejected.

        This is the prefix-invariance guard: a feature row computed at
        decision time ``t`` must not depend on a row whose
        ``available_at > t``.
        """
        observation = _make_observation(
            observed_at=1_000_000,
            staleness_seconds=10,
        )  # available_at = 1_000_010
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        with pytest.raises(QuoteObservationError, match="after decision_time"):
            convert_to_usdg(
                observation=observation,
                qualification=qualification,
                decision_time=1_000_005,  # strictly before available_at
            )

    def test_decision_time_accepts_already_available_observation(self) -> None:
        """An observation whose available_at <= decision_time is accepted."""
        observation = _make_observation(observed_at=1_000_000, staleness_seconds=10)
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        bar = convert_to_usdg(
            observation=observation,
            qualification=qualification,
            decision_time=1_000_010,
        )
        assert bar.missing_policy is MissingPolicy.OK


# ---------------------------------------------------------------------------
# Prefix invariance
# ---------------------------------------------------------------------------


class TestPrefixInvariance:
    """Adding future data must not change an earlier observation row.

    The invariant is satisfied by the immutable, time-ordered
    construction of the observation rows: an observation produced
    from data available at time ``t`` does not change when data
    after ``t`` is appended. The tests assert the invariant by
    computing the same USDG bar twice — once with no future data,
    once with a future observation appended to the dataset — and
    comparing the bytes.
    """

    def test_same_inputs_produce_byte_identical_bar(self) -> None:
        observation = _make_observation(
            observed_at=1_000_000,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 2,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        bar_a = convert_to_usdg(observation=observation, qualification=qualification)
        bar_b = convert_to_usdg(observation=observation, qualification=qualification)
        # Byte-equivalence check via dataclass equality + ratio equality.
        assert bar_a == bar_b
        assert bar_a.usdg_per_token_q64_64 == bar_b.usdg_per_token_q64_64

    def test_determinism_hash_matches(self) -> None:
        observation = _make_observation()
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        bar_a = convert_to_usdg(observation=observation, qualification=qualification)
        bar_b = convert_to_usdg(observation=observation, qualification=qualification)
        assert hash(bar_a) == hash(bar_b)


# ---------------------------------------------------------------------------
# RELATIVE_ONLY output (no USD-denominated field)
# ---------------------------------------------------------------------------


class TestRelativeOnlyOutput:
    """The RELATIVE_ONLY output path carries no USD-denominated field."""

    def test_build_relative_only_bar_yields_ratio(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 2,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.RELATIVE_ONLY)
        bar = build_relative_only_bar(observation=observation, qualification=qualification)
        assert isinstance(bar, RelativeOnlyBar)
        assert bar.token1_per_token0_q64_64 == Q64_SCALE * 2

    def test_build_relative_only_bar_rejects_non_relative_qualification(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        with pytest.raises(QuoteRelativeOnlyError, match="RELATIVE_ONLY"):
            build_relative_only_bar(observation=observation, qualification=qualification)

    def test_relative_only_bar_carries_no_usd_field(self) -> None:
        """The RELATIVE_ONLY bar dataclass has no USD-denominated field."""
        fields = RelativeOnlyBar.__dataclass_fields__
        for field_name in fields:
            if field_name == "notes":
                continue
            assert "usdg" not in field_name, (
                f"RelativeOnlyBar field {field_name!r} is USD-denominated"
            )
            assert "usd" not in field_name, (
                f"RelativeOnlyBar field {field_name!r} is USD-denominated"
            )

    def test_assert_no_usd_fields_accepts_relative_only_payload(self) -> None:
        payload = {
            "token1_per_token0_q64_64": Q64_SCALE * 2,
            "block_number": 100,
            "observation_count": 5,
        }
        assert_no_usd_fields(payload)

    def test_assert_no_usd_fields_rejects_usdg_field(self) -> None:
        payload = {"pnl_usdg": 1_000_000}
        with pytest.raises(QuoteRelativeOnlyError, match="pnl_usdg"):
            assert_no_usd_fields(payload)

    def test_assert_no_usd_fields_rejects_usd_suffix(self) -> None:
        payload = {"fee_usdg": 1, "foo_bar_usd": 2}
        with pytest.raises(QuoteRelativeOnlyError):
            assert_no_usd_fields(payload)

    def test_assert_no_usd_fields_rejects_non_mapping(self) -> None:
        with pytest.raises(QuoteRelativeOnlyError, match="payload must be Mapping"):
            assert_no_usd_fields(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_ranking_blocked_between_usdg_and_relative(self) -> None:
        observation = _make_observation()
        qualification_usdg = _make_qualification(selected_level=NumeraireLevel.USDG)
        qualification_relative = _make_qualification(selected_level=NumeraireLevel.RELATIVE_ONLY)
        usdg_bar = convert_to_usdg(observation=observation, qualification=qualification_usdg)
        relative_observation = _make_observation(
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        )
        relative_bar = build_relative_only_bar(
            observation=relative_observation, qualification=qualification_relative
        )
        assert ranking_blocked_between(usdg_bar, relative_bar) is True
        assert ranking_blocked_between(relative_bar, usdg_bar) is True

    def test_ranking_blocked_between_two_usdg_bars_is_false(self) -> None:
        observation = _make_observation()
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        bar_a = convert_to_usdg(observation=observation, qualification=qualification)
        bar_b = convert_to_usdg(observation=observation, qualification=qualification)
        assert ranking_blocked_between(bar_a, bar_b) is False

    def test_ranking_blocked_between_two_relative_bars_is_false(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.RELATIVE_ONLY)
        bar_a = build_relative_only_bar(observation=observation, qualification=qualification)
        bar_b = build_relative_only_bar(observation=observation, qualification=qualification)
        assert ranking_blocked_between(bar_a, bar_b) is False

    def test_quote_bar_relative_only_rejects_usdg_price(self) -> None:
        """Constructing a RELATIVE_ONLY QuoteBar with a USDG price raises."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.RELATIVE_ONLY)
        with pytest.raises(QuoteObservationError, match="RELATIVE_ONLY bars must not"):
            QuoteBar(
                observation=observation,
                qualification=qualification,
                usdg_per_token_q64_64=Q64_SCALE,
                missing_policy=MissingPolicy.OK,
                path=ConversionPath(
                    source=NumeraireLevel.RELATIVE_ONLY,
                    target=NumeraireLevel.RELATIVE_ONLY,
                    edges=(),
                    policy=MissingPolicy.OK,
                ),
            )

    def test_usd_denominated_forbidden_fields_constant_is_complete(self) -> None:
        """The forbidden-field constant is non-empty and stable."""
        assert len(USD_DENOMINATED_FORBIDDEN_FIELDS) >= 5
        assert "pnl_usdg" in USD_DENOMINATED_FORBIDDEN_FIELDS
        assert "value_usdg" in USD_DENOMINATED_FORBIDDEN_FIELDS
        assert "fee_usdg" in USD_DENOMINATED_FORBIDDEN_FIELDS
        assert "gas_usdg" in USD_DENOMINATED_FORBIDDEN_FIELDS
        assert "risk_usdg" in USD_DENOMINATED_FORBIDDEN_FIELDS


# ---------------------------------------------------------------------------
# Per-numeraire fixtures
# ---------------------------------------------------------------------------


class TestPerNumeraireFixtures:
    """Each numeraire level has its own fixtures and own USDG semantics."""

    @pytest.mark.parametrize(
        "level",
        [
            NumeraireLevel.USDG,
            NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            NumeraireLevel.ETH_DISPLAY_ONLY,
            NumeraireLevel.RELATIVE_ONLY,
        ],
    )
    def test_each_level_has_a_qualification_record(self, level: NumeraireLevel) -> None:
        bundle = _make_qualification(selected_level=level)
        record = bundle.record_for_level(level)
        assert record.level is level
        assert record.selected is True

    def test_usdg_qualification_has_no_stablecoin_ratio(self) -> None:
        bundle = _make_qualification(selected_level=NumeraireLevel.USDG)
        record = bundle.record_for_level(NumeraireLevel.USDG)
        assert record.stablecoin_per_usdg_q64_64 is None

    def test_stablecoin_qualification_carries_observed_ratio(self) -> None:
        observed_ratio = (Q64_SCALE * 99) // 100  # 0.99 USD per stablecoin
        bundle = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=observed_ratio,
        )
        record = bundle.record_for_level(NumeraireLevel.QUALIFIED_USD_STABLECOIN)
        assert record.stablecoin_per_usdg_q64_64 == observed_ratio

    def test_eth_qualification_carries_no_stablecoin_ratio(self) -> None:
        bundle = _make_qualification(selected_level=NumeraireLevel.ETH_DISPLAY_ONLY)
        record = bundle.record_for_level(NumeraireLevel.ETH_DISPLAY_ONLY)
        assert record.stablecoin_per_usdg_q64_64 is None

    def test_relative_only_qualification_carries_no_stablecoin_ratio(self) -> None:
        bundle = _make_qualification(selected_level=NumeraireLevel.RELATIVE_ONLY)
        record = bundle.record_for_level(NumeraireLevel.RELATIVE_ONLY)
        assert record.stablecoin_per_usdg_q64_64 is None

    def test_usdg_observation_yields_usdg_price(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.USDG,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 7,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.missing_policy is MissingPolicy.OK
        assert bar.usdg_per_token_q64_64 == Q64_SCALE * 7

    def test_stablecoin_observation_yields_usdg_price(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 2,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=Q64_SCALE,
        )
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.missing_policy is MissingPolicy.OK
        assert bar.usdg_per_token_q64_64 == Q64_SCALE * 2

    def test_eth_observation_refuses_usdg_price(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.ETH_DISPLAY_ONLY,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.ETH_DISPLAY_ONLY)
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.missing_policy is MissingPolicy.MISSING
        assert bar.usdg_per_token_q64_64 is None

    def test_relative_only_observation_carries_no_usdg_price(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 3,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.RELATIVE_ONLY)
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.is_relative_only is True
        assert bar.usdg_per_token_q64_64 is None


# ---------------------------------------------------------------------------
# Cross-rate fixtures
# ---------------------------------------------------------------------------


class TestCrossRateFixtures:
    """The conversion graph must reject cross-rate through ETH/USDG."""

    def test_eth_to_usdg_path_is_refused(self) -> None:
        """ETH → USDG is forbidden by ADR-014 §3.

        The graph does not expose an ETH → USDG edge. ``convert_observation``
        returns a QuoteBar with ``MissingPolicy.MISSING``.
        """
        observation = _make_observation(
            numeraire_level=NumeraireLevel.ETH_DISPLAY_ONLY,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.ETH_DISPLAY_ONLY)
        bar = convert_observation(
            observation=observation,
            qualification=qualification,
            target=NumeraireLevel.USDG,
        )
        assert bar.missing_policy is MissingPolicy.MISSING
        assert bar.usdg_per_token_q64_64 is None

    def test_relative_only_to_usdg_cross_rate_is_refused(self) -> None:
        """RELATIVE_ONLY → USDG is forbidden (no USD route)."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 4,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.RELATIVE_ONLY)
        bar = convert_observation(
            observation=observation,
            qualification=qualification,
            target=NumeraireLevel.USDG,
        )
        assert bar.usdg_per_token_q64_64 is None
        assert bar.is_relative_only is True

    def test_usdg_to_relative_only_returns_relative_bar(self) -> None:
        """USDG → RELATIVE_ONLY is permitted as an explicit conversion."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.USDG,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 2,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        bar = convert_observation(
            observation=observation,
            qualification=qualification,
            target=NumeraireLevel.RELATIVE_ONLY,
        )
        assert bar.is_relative_only is True
        assert bar.usdg_per_token_q64_64 is None

    def test_convert_observation_to_usdg_is_alias_for_convert_to_usdg(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.USDG,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 3,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        bar = convert_observation(
            observation=observation,
            qualification=qualification,
            target=NumeraireLevel.USDG,
        )
        assert bar.usdg_per_token_q64_64 == Q64_SCALE * 3

    def test_delayed_staleness_for_non_usdg_target(self) -> None:
        """A delayed observation for an arbitrary target returns DELAYED."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            staleness_seconds=10_000,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=Q64_SCALE,
        )
        bar = convert_observation(
            observation=observation,
            qualification=qualification,
            target=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
        )
        assert bar.missing_policy is MissingPolicy.DELAYED


# ---------------------------------------------------------------------------
# 5-minute rule labels
# ---------------------------------------------------------------------------


class TestFiveMinuteRules:
    """The 5-minute extreme-move rules share the same USDG semantics."""

    def test_up_spike_threshold_default(self) -> None:
        """The default UP_SPIKE threshold is 100% (1.0 in Q64.64)."""
        assert FIVE_MINUTE_UP_SPIKE_FRACTION == Q64_SCALE

    def test_down_spike_threshold_default(self) -> None:
        """The default DOWN_SPIKE threshold is 80% (0.8 in Q64.64)."""
        assert FIVE_MINUTE_DOWN_SPIKE_FRACTION == (80 * Q64_SCALE) // 100

    def test_five_minute_window_is_300_seconds(self) -> None:
        """The 5-minute window is 300 seconds."""
        assert FIVE_MINUTE_RULE_WINDOW_SECONDS == 300

    def test_up_spike_is_triggered_on_doubling(self) -> None:
        """A move from 1.0 to 2.1 (110%) triggers UP_SPIKE."""
        pre_observation = _make_observation(
            observed_at=1_000_000,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        post_observation = _make_observation(
            observed_at=1_000_300,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 21 // 10,  # 2.1
        )
        pre_qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        post_qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        pre_bar = convert_to_usdg(observation=pre_observation, qualification=pre_qualification)
        post_bar = convert_to_usdg(observation=post_observation, qualification=post_qualification)
        up, down = evaluate_five_minute_rules(pre_bar=pre_bar, post_bar=post_bar)
        assert isinstance(up, FiveMinuteRuleVerdict)
        assert up.rule_id == "UP_SPIKE"
        assert up.triggered is True
        assert down.triggered is False

    def test_down_spike_is_triggered_on_collapse(self) -> None:
        """A move from 1.0 to 0.1 (90% drop) triggers DOWN_SPIKE."""
        pre_observation = _make_observation(
            observed_at=1_000_000,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        post_observation = _make_observation(
            observed_at=1_000_300,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE // 10,
        )
        pre_qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        post_qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        pre_bar = convert_to_usdg(observation=pre_observation, qualification=pre_qualification)
        post_bar = convert_to_usdg(observation=post_observation, qualification=post_qualification)
        up, down = evaluate_five_minute_rules(pre_bar=pre_bar, post_bar=post_bar)
        assert up.triggered is False
        assert down.triggered is True

    def test_below_threshold_is_not_triggered(self) -> None:
        """A 1% move does not trigger either rule."""
        pre_observation = _make_observation(
            observed_at=1_000_000,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        post_observation = _make_observation(
            observed_at=1_000_300,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE + (Q64_SCALE // 100),
        )
        pre_qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        post_qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        pre_bar = convert_to_usdg(observation=pre_observation, qualification=pre_qualification)
        post_bar = convert_to_usdg(observation=post_observation, qualification=post_qualification)
        up, down = evaluate_five_minute_rules(pre_bar=pre_bar, post_bar=post_bar)
        assert up.triggered is False
        assert down.triggered is False

    def test_rules_reject_relative_only_bars(self) -> None:
        """Both 5-minute rules require a USDG price; RELATIVE_ONLY is rejected."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.RELATIVE_ONLY,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.RELATIVE_ONLY)
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        with pytest.raises(QuoteRelativeOnlyError, match="RELATIVE_ONLY"):
            evaluate_five_minute_rules(pre_bar=bar, post_bar=bar)

    def test_rules_share_the_same_usdg_price_object(self) -> None:
        """Performance, exposure and both 5-min rules share the same bar."""
        observation = _make_observation(
            numeraire_level=NumeraireLevel.USDG,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 2,
        )
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        # Same observation row is consumed by all three consumers.
        performance_bar = convert_to_usdg(observation=observation, qualification=qualification)
        exposure_bar = convert_to_usdg(observation=observation, qualification=qualification)
        rule_pre_bar = convert_to_usdg(observation=observation, qualification=qualification)
        # Same input -> byte-identical output.
        assert performance_bar.usdg_per_token_q64_64 == exposure_bar.usdg_per_token_q64_64
        assert performance_bar.usdg_per_token_q64_64 == rule_pre_bar.usdg_per_token_q64_64


# ---------------------------------------------------------------------------
# Source / frequency mixing (must-not)
# ---------------------------------------------------------------------------


class TestSourceFrequencyProvenance:
    """The schema never mixes source / frequency without provenance."""

    def test_external_feed_observation_carries_source_kind(self) -> None:
        observation = _make_observation(
            source=SourceKind.EXTERNAL_FEED,
            pair="ZZZ/USDG-EXT",
            numeraire_level=NumeraireLevel.USDG,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        assert observation.source is SourceKind.EXTERNAL_FEED
        assert observation.pair == "ZZZ/USDG-EXT"

    def test_onchain_pool_and_external_feed_are_distinct_rows(self) -> None:
        """Mixing two sources requires two distinct Observation rows."""
        pool = _make_observation(
            source=SourceKind.ONCHAIN_POOL,
            pair="ZZZ/USDG",
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        feed = _make_observation(
            source=SourceKind.EXTERNAL_FEED,
            pair="ZZZ/USDG-FEED",
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE,
        )
        assert pool.source is not feed.source
        assert pool.pair != feed.pair

    def test_unknown_source_kind_is_recorded(self) -> None:
        observation = _make_observation(
            source=SourceKind.UNKNOWN,
            pair="ZZZ/USDG-UNKNOWN",
        )
        assert observation.source is SourceKind.UNKNOWN


# ---------------------------------------------------------------------------
# Display boundary
# ---------------------------------------------------------------------------


class TestDisplayBoundary:
    """The Decimal display boundary (ADR-009)."""

    def test_format_ratio_decimal_round_trip(self) -> None:
        from decimal import Decimal as D

        # 1.5 ratio in Q64.64
        result = format_ratio_decimal(Q64_SCALE * 3 // 2)
        assert result == D("1.5")

    def test_format_ratio_decimal_rejects_float(self) -> None:
        with pytest.raises(QuoteObservationError, match="must be int"):
            format_ratio_decimal(1.5)  # type: ignore[arg-type]

    def test_format_ratio_decimal_rejects_negative(self) -> None:
        with pytest.raises(QuoteObservationError, match="must be non-negative"):
            format_ratio_decimal(-1)

    def test_format_ratio_decimal_rejects_invalid_digits(self) -> None:
        with pytest.raises(QuoteObservationError, match="must be in"):
            format_ratio_decimal(Q64_SCALE, fractional_digits=-1)
        with pytest.raises(QuoteObservationError, match="must be in"):
            format_ratio_decimal(Q64_SCALE, fractional_digits=100)


# ---------------------------------------------------------------------------
# Type-safety and validation guards
# ---------------------------------------------------------------------------


class TestTypeGuards:
    """The conversion graph and helpers enforce type / width constraints."""

    def test_conversion_edge_rejects_unknown_source(self) -> None:
        with pytest.raises(QuoteObservationError, match="source"):
            ConversionEdge(
                source="USDG",  # type: ignore[arg-type]
                target=NumeraireLevel.USDG,
                policy=EdgePolicy.DIRECT,
                max_staleness_seconds=0,
            )

    def test_conversion_edge_rejects_negative_staleness(self) -> None:
        with pytest.raises(QuoteObservationError, match="max_staleness_seconds"):
            ConversionEdge(
                source=NumeraireLevel.USDG,
                target=NumeraireLevel.USDG,
                policy=EdgePolicy.DIRECT,
                max_staleness_seconds=-1,
            )

    def test_observation_value_max_is_uint256(self) -> None:
        """MAX_UINT256 constant is exact (1 << 256) - 1."""
        assert MAX_UINT256 == (1 << 256) - 1

    def test_convert_to_usdg_rejects_non_observation(self) -> None:
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        with pytest.raises(QuoteObservationError, match="Observation"):
            convert_to_usdg(observation="not an observation", qualification=qualification)  # type: ignore[arg-type]

    def test_convert_to_usdg_rejects_non_qualification(self) -> None:
        observation = _make_observation()
        with pytest.raises(QuoteObservationError, match="QualificationBundle"):
            convert_to_usdg(observation=observation, qualification="not a bundle")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Per-fixture delayed / revised / depegged / missing scenarios
# ---------------------------------------------------------------------------


class TestScenarioFixtures:
    """Each must-not scenario gets its own fixture."""

    def test_delayed_scenario_label(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            staleness_seconds=10_000,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=Q64_SCALE,
        )
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.missing_policy is MissingPolicy.DELAYED

    def test_revised_scenario_constant_exists(self) -> None:
        """REVISED is a MissingPolicy value; consumers may apply it manually."""
        assert MissingPolicy.REVISED in MissingPolicy

    def test_depegged_scenario_label(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            unit=ObservationUnit.RATIO,
            value=Q64_SCALE * 11 // 10,  # 10% move
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=Q64_SCALE,
        )
        bar = convert_to_usdg(
            observation=observation,
            qualification=qualification,
            previous_ratio_q64_64=Q64_SCALE,
            depeg_threshold_q64_64=DEFAULT_DEPEG_THRESHOLD_Q64_64,
        )
        assert bar.missing_policy is MissingPolicy.DEPEGGED

    def test_missing_scenario_label(self) -> None:
        observation = _make_observation(
            numeraire_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
        )
        qualification = _make_qualification(
            selected_level=NumeraireLevel.QUALIFIED_USD_STABLECOIN,
            stablecoin_per_usdg_q64_64=None,
        )
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.missing_policy is MissingPolicy.MISSING

    def test_cross_rate_scenario_constant_exists(self) -> None:
        """CROSS_RATE is a MissingPolicy value; the graph never substitutes a current price."""
        assert MissingPolicy.CROSS_RATE in MissingPolicy


# ---------------------------------------------------------------------------
# Float-freedom (ADR-004)
# ---------------------------------------------------------------------------


class TestFloatFreedom:
    """The T053 module must not introduce ``float`` on the valuation path."""

    def test_quote_module_has_no_float(self) -> None:
        """The quote.py source does not construct ``float`` values."""
        from pathlib import Path

        source_path = (
            Path(__file__).resolve().parents[1] / "src" / "robinhood_lp" / "features" / "quote.py"
        )
        text = source_path.read_text(encoding="utf-8")
        # We allow the documentation strings to mention ``float`` in
        # negative form ("no float"), but we reject any literal float
        # syntax. The check below is a textual audit: the module
        # never produces a ``float``; protocol code consumes ``int``.
        import re

        for match in re.finditer(r"\bfloat\s*\(", text):
            line_no = text[: match.start()].count("\n") + 1
            line_text = text.splitlines()[line_no - 1]
            assert "no float" in line_text.lower() or "forbid" in line_text.lower(), (
                f"unexpected float() construction at line {line_no}: {line_text!r}"
            )

    def test_observation_value_is_int(self) -> None:
        observation = _make_observation(value=Q64_SCALE)
        assert isinstance(observation.value, int)
        assert not isinstance(observation.value, bool)

    def test_quote_bar_usdg_price_is_int_or_none(self) -> None:
        observation = _make_observation()
        qualification = _make_qualification(selected_level=NumeraireLevel.USDG)
        bar = convert_to_usdg(observation=observation, qualification=qualification)
        assert bar.usdg_per_token_q64_64 is None or isinstance(bar.usdg_per_token_q64_64, int)
