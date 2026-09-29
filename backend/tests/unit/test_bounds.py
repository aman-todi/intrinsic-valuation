"""Ticket 8: bounds rules (spec §5.6), table-driven per rule family: each row passes or names the
violated field."""

from collections.abc import Callable

from pydantic import BaseModel

from app.assumptions.bounds import check_bounds, describe_bounds
from app.schemas.assumptions import (
    FCFEAssumptions,
    FCFFAssumptions,
    SotpAssumptions,
    SotpSegmentAssumption,
)
from app.schemas.company import ModelType
from tests.unit.proposer_fixtures import (
    af,
    valid_ep,
    valid_excess_return,
    valid_fcfe,
    valid_fcff,
    valid_reit,
    valid_segment_multiple,
)

Factory = Callable[..., BaseModel]


BASELINES: list[tuple[ModelType, Factory]] = [
    (ModelType.FCFF, valid_fcff),
    (ModelType.FCFE, valid_fcfe),
    (ModelType.EXCESS_RETURN, valid_excess_return),
    (ModelType.NAV_REIT, valid_reit),
    (ModelType.NAV_EP, valid_ep),
    (ModelType.SOTP, valid_segment_multiple),
    (ModelType.SOTP, valid_fcff),
]


def test_valid_baselines_pass() -> None:
    for model_type, factory in BASELINES:
        assert check_bounds(model_type, factory()) == [], (model_type, factory.__name__)


# --------------------------------------------------------------------------- single-field ranges
# (model_type, factory, field, passing edge values, violating values)

RANGE_CASES: list[tuple[ModelType, Factory, str, list[float], list[float]]] = [
    # FCFF
    *[(ModelType.FCFF, valid_fcff, f"revenue_growth_y{i}", [-0.5, 0.0], [-0.51, 1.51]) for i in range(1, 6)],
    (ModelType.FCFF, valid_fcff, "target_operating_margin", [-1.0, 0.8], [-1.01, 0.81]),
    (ModelType.FCFF, valid_fcff, "margin_convergence_years", [1, 10], [0.5, 11]),
    (ModelType.FCFF, valid_fcff, "tax_rate", [0.0, 0.5], [-0.01, 0.51]),
    (ModelType.FCFF, valid_fcff, "sales_to_capital_ratio", [0.01, 20], [0.0, -1.0, 20.5]),
    (ModelType.FCFF, valid_fcff, "equity_risk_premium", [0.02, 0.10], [0.019, 0.11]),
    (ModelType.FCFF, valid_fcff, "levered_beta", [0.3, 3.5], [0.29, 3.6]),
    (ModelType.FCFF, valid_fcff, "pretax_cost_of_debt", [0.0, 0.25], [-0.01, 0.26]),
    (ModelType.FCFF, valid_fcff, "target_debt_to_capital", [0.0, 0.9], [-0.01, 0.91]),
    (ModelType.FCFF, valid_fcff, "terminal_roic", [0.05, 1.0], [0.0, -0.1, 1.1]),
    (ModelType.FCFF, valid_fcff, "survival_probability", [1.0, 0.85], [0.0, -0.2, 1.01]),
    # FCFE
    *[(ModelType.FCFE, valid_fcfe, f"revenue_growth_y{i}", [-0.5, 1.5], [-0.6, 1.6]) for i in range(1, 6)],
    (ModelType.FCFE, valid_fcfe, "target_net_margin", [-1.0, 0.8], [-1.1, 0.9]),
    (ModelType.FCFE, valid_fcfe, "margin_convergence_years", [1, 10], [0, 12]),
    (ModelType.FCFE, valid_fcfe, "tax_rate", [0.0, 0.5], [-0.1, 0.6]),
    (ModelType.FCFE, valid_fcfe, "target_debt_to_capital", [0.0, 0.9], [-0.1, 0.95]),
    (ModelType.FCFE, valid_fcfe, "net_borrowing_as_pct_reinvestment", [0.0, 1.0], [-0.01, 1.01]),
    (ModelType.FCFE, valid_fcfe, "equity_risk_premium", [0.02, 0.10], [0.01, 0.12]),
    (ModelType.FCFE, valid_fcfe, "levered_beta", [0.3, 3.5], [0.2, 4.0]),
    # Excess return
    *[
        (ModelType.EXCESS_RETURN, valid_excess_return, f"roe_y{i}", [0.05, 0.2], [-0.31, 0.51])
        for i in range(1, 6)
    ],
    (ModelType.EXCESS_RETURN, valid_excess_return, "terminal_roe", [-0.3, 0.5], [-0.31, 0.51]),
    (ModelType.EXCESS_RETURN, valid_excess_return, "cost_of_equity", [0.05, 0.20], [0.049, 0.21]),
    (ModelType.EXCESS_RETURN, valid_excess_return, "terminal_growth_rate", [-0.02, 0.04], [-0.03]),
    # REIT
    (ModelType.NAV_REIT, valid_reit, "cap_rate", [0.03, 0.12], [0.029, 0.121, 0.0]),
    (ModelType.NAV_REIT, valid_reit, "noi_growth_rate", [-0.1, 0.15], [-0.11, 0.16]),
    (ModelType.NAV_REIT, valid_reit, "liability_adjustment", [0.0, 5e10], [-1.0]),
    (ModelType.NAV_REIT, valid_reit, "non_real_estate_asset_adjustment", [0.0, 1e9], [-1.0]),
    # E&P
    (ModelType.NAV_EP, valid_ep, "price_deck_oil_per_bbl", [20.0, 200.0], [19.0, 201.0, 0.0]),
    (ModelType.NAV_EP, valid_ep, "price_deck_gas_per_mcf", [0.5, 20.0], [0.4, 21.0, -1.0]),
    (ModelType.NAV_EP, valid_ep, "discount_rate_pv10", [0.05, 0.20], [0.04, 0.21]),
    (ModelType.NAV_EP, valid_ep, "development_cost_adjustment", [0.0, 1e9], [-1e6]),
    # SOTP segment multiple
    (ModelType.SOTP, valid_segment_multiple, "ev_ebitda_multiple", [0.1, 40.0], [0.0, -5.0, 41.0]),
    (ModelType.SOTP, valid_segment_multiple, "segment_ebitda_margin", [-1.0, 0.8], [-1.1, 0.9]),
]


def test_single_field_ranges() -> None:
    for model_type, factory, field, ok, bad in RANGE_CASES:
        for v in ok:
            assert check_bounds(model_type, factory(**{field: v})) == [], (model_type, field, v)
        for v in [*bad, float("nan")]:
            violations = check_bounds(model_type, factory(**{field: v}))
            assert any(field in msg for msg in violations), (model_type, field, v, violations)


# --------------------------------------------------------------------------- cross-field rules
# (model_type, proposal, check_bounds kwargs, expected substring of a violation or None for "passes")

LOW_WACC = dict(
    risk_free_rate=0.03,
    levered_beta=0.3,
    equity_risk_premium=0.02,
    pretax_cost_of_debt=0.03,
    target_debt_to_capital=0.9,
    tax_rate=0.4,
    terminal_growth_rate=0.028,
)
ER, FCFF, FCFE = ModelType.EXCESS_RETURN, ModelType.FCFF, ModelType.FCFE
CROSS_FIELD_CASES: list[tuple[ModelType, Callable[[], BaseModel], dict, str | None]] = [
    # risk-free range and terminal growth <= rf
    (FCFF, lambda: valid_fcff(risk_free_rate=0.10, terminal_growth_rate=0.02), {}, None),
    (FCFE, lambda: valid_fcfe(risk_free_rate=0.11, terminal_growth_rate=0.02), {}, "risk_free_rate"),
    (FCFF, lambda: valid_fcff(terminal_growth_rate=0.042), {}, None),  # == rf is OK
    (FCFF, lambda: valid_fcff(terminal_growth_rate=0.045), {}, "must be <= risk_free_rate"),
    (FCFE, lambda: valid_fcfe(terminal_growth_rate=-0.03), {}, "terminal_growth_rate"),
    # discount rate must exceed terminal growth by the engine's 0.5pt spread
    (FCFF, lambda: valid_fcff(**LOW_WACC), {}, "WACC"),
    (
        FCFE,
        lambda: valid_fcfe(risk_free_rate=0.03, levered_beta=0.0, terminal_growth_rate=0.03),
        {},
        "cost of equity",
    ),
    (ER, lambda: valid_excess_return(cost_of_equity=0.06, terminal_growth_rate=0.05), {}, None),
    (
        ER,
        lambda: valid_excess_return(cost_of_equity=0.054, terminal_growth_rate=0.05),
        {},
        "by more than 0.005",
    ),
    (
        ER,
        lambda: valid_excess_return(terminal_growth_rate=0.045),
        {"risk_free_rate": 0.04},
        "must be <= risk_free_rate",
    ),
    # terminal ROIC vs growth, survival only for early stage, early-stage growth cap 300%
    (FCFF, lambda: valid_fcff(terminal_roic=0.02, terminal_growth_rate=0.025), {}, "reinvestment"),
    (FCFF, lambda: valid_fcff(terminal_roic=0.02, terminal_growth_rate=0.0), {}, None),
    (FCFF, lambda: valid_fcff(survival_probability=0.7), {}, "early-stage"),
    (FCFF, lambda: valid_fcff(survival_probability=0.7), {"early_stage": True}, None),
    (FCFF, lambda: valid_fcff(revenue_growth_y1=3.0, survival_probability=0.6), {"early_stage": True}, None),
    (FCFF, lambda: valid_fcff(revenue_growth_y1=3.1), {"early_stage": True}, "revenue_growth_y1"),
    (FCFF, lambda: valid_fcff(revenue_growth_y1=2.5), {}, "revenue_growth_y1"),
    # excess return payout / book-value-growth consistency
    (ER, lambda: valid_excess_return(payout_ratio=1.0, book_value_growth_rate=0.0), {}, None),
    (ER, lambda: valid_excess_return(payout_ratio=1.1, book_value_growth_rate=-0.01), {}, "payout_ratio"),
    (ER, lambda: valid_excess_return(book_value_growth_rate=0.2), {}, "inconsistent"),
    # dispatch
    (ModelType.NAV_REIT, valid_fcff, {}, "expected ReitNavAssumptions"),
    (FCFF, valid_segment_multiple, {}, "only valid for sotp"),
]


def test_cross_field_rules() -> None:
    for i, (model_type, make, kwargs, expected) in enumerate(CROSS_FIELD_CASES):
        v = check_bounds(model_type, make(), **kwargs)
        if expected is None:
            assert v == [], (i, v)
        else:
            assert any(expected in m for m in v), (i, expected, v)


# --------------------------------------------------------------------------- SOTP


def _sotp(
    segments: list[SotpSegmentAssumption], overhead: float = -1e9, consolidated: FCFFAssumptions | None = None
) -> SotpAssumptions:
    return SotpAssumptions(
        segments=segments,
        corporate_overhead_capitalized=af(overhead),
        conglomerate_discount_note="n/a",
        consolidated_fcff=consolidated,
    )


def _fcff_seg(name: str, p: FCFFAssumptions | None = None) -> SotpSegmentAssumption:
    return SotpSegmentAssumption(
        segment_name=name, valuation_approach="fcff", ev_ebitda_multiple=0, fcff_assumptions=p or valid_fcff()
    )


def _mult_seg(name: str, multiple: float = 10.0, margin: float | None = 0.2) -> SotpSegmentAssumption:
    return SotpSegmentAssumption(
        segment_name=name,
        valuation_approach="ev_ebitda_multiple",
        ev_ebitda_multiple=multiple,
        segment_ebitda_margin=margin,
    )


def test_sotp_rules() -> None:
    ok = _sotp([_fcff_seg("A"), _mult_seg("B"), _mult_seg("C", margin=None)], consolidated=valid_fcff())
    assert check_bounds(ModelType.SOTP, ok) == []
    assert check_bounds(ModelType.SOTP, _sotp([_mult_seg("B", multiple=40.0)])) == []
    # segment and consolidated FCFF proposals are checked recursively, with a prefixed field path
    bad_seg = _fcff_seg("A", valid_fcff(terminal_growth_rate=0.08))
    v = check_bounds(ModelType.SOTP, _sotp([bad_seg, _mult_seg("B")]))
    assert v and all(m.startswith("segment[A].") for m in v)
    v = check_bounds(ModelType.SOTP, _sotp([_mult_seg("B")], consolidated=valid_fcff(tax_rate=0.7)))
    assert any(m.startswith("consolidated_fcff.tax_rate") for m in v)
    missing = SotpSegmentAssumption(segment_name="A", valuation_approach="fcff", ev_ebitda_multiple=0)
    weird = SotpSegmentAssumption(segment_name="A", valuation_approach="dcf", ev_ebitda_multiple=5)
    violating = [
        (_sotp([_mult_seg("B", multiple=40.5)]), "segment[B].ev_ebitda_multiple"),
        (_sotp([_mult_seg("B", multiple=0.0)]), "segment[B].ev_ebitda_multiple"),
        (_sotp([_mult_seg("B", margin=0.95)]), "segment_ebitda_margin"),
        (_sotp([]), "segments must not be empty"),
        (_sotp([_mult_seg("B"), _mult_seg("B")]), "unique"),
        (_sotp([missing]), "fcff_assumptions required"),
        (_sotp([weird]), "valuation_approach"),
        (_sotp([_mult_seg("B")], overhead=5e8), "corporate_overhead_capitalized"),
    ]
    for proposal, expected in violating:
        v = check_bounds(ModelType.SOTP, proposal)
        assert any(expected in m for m in v), (expected, v)


def test_describe_bounds_mentions_every_ranged_field() -> None:
    for schema_cls in (FCFFAssumptions, FCFEAssumptions):
        text = "\n".join(describe_bounds(schema_cls))
        for name in schema_cls.model_fields:
            assert name in text, name
    assert "3" in "\n".join(describe_bounds(FCFFAssumptions, early_stage=True))
