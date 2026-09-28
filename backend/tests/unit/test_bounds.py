"""Ticket 8: every bounds rule, passing and violating (spec §5.6)."""

from collections.abc import Callable

import pytest
from pydantic import BaseModel

from app.assumptions import bounds
from app.assumptions.bounds import check_bounds, describe_bounds, fcff_wacc
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


# --------------------------------------------------------------------------- baselines pass


@pytest.mark.parametrize(
    ("model_type", "factory"),
    [
        (ModelType.FCFF, valid_fcff),
        (ModelType.FCFE, valid_fcfe),
        (ModelType.EXCESS_RETURN, valid_excess_return),
        (ModelType.NAV_REIT, valid_reit),
        (ModelType.NAV_EP, valid_ep),
        (ModelType.SOTP, valid_segment_multiple),
        (ModelType.SOTP, valid_fcff),
    ],
)
def test_valid_baselines_pass(model_type: ModelType, factory: Factory) -> None:
    assert check_bounds(model_type, factory()) == []


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


def _case_ids() -> list[str]:
    return [f"{mt.value}-{field}" for mt, _, field, _, _ in RANGE_CASES]


@pytest.mark.parametrize(("model_type", "factory", "field", "ok", "bad"), RANGE_CASES, ids=_case_ids())
def test_range_rules(
    model_type: ModelType, factory: Factory, field: str, ok: list[float], bad: list[float]
) -> None:
    for v in ok:
        assert check_bounds(model_type, factory(**{field: v})) == [], (field, v)
    for v in bad:
        violations = check_bounds(model_type, factory(**{field: v}))
        assert any(field in msg for msg in violations), (field, v, violations)


@pytest.mark.parametrize("model_type", [ModelType.FCFF, ModelType.FCFE])
def test_risk_free_range(model_type: ModelType) -> None:
    factory = valid_fcff if model_type is ModelType.FCFF else valid_fcfe
    assert check_bounds(model_type, factory(risk_free_rate=0.10, terminal_growth_rate=0.02)) == []
    assert check_bounds(model_type, factory(risk_free_rate=0.03, terminal_growth_rate=0.02)) == []
    v = check_bounds(model_type, factory(risk_free_rate=-0.01, terminal_growth_rate=-0.02))
    assert any("risk_free_rate" in m and "out of bounds" in m for m in v)
    v = check_bounds(model_type, factory(risk_free_rate=0.11, terminal_growth_rate=0.02))
    assert any("risk_free_rate" in m and "out of bounds" in m for m in v)


def test_growth_early_stage_allows_up_to_300pct() -> None:
    p = valid_fcff(revenue_growth_y1=2.5, survival_probability=0.6)
    assert check_bounds(ModelType.FCFF, p, early_stage=True) == []
    assert check_bounds(ModelType.FCFF, valid_fcff(revenue_growth_y1=3.0), early_stage=True) == []
    assert check_bounds(ModelType.FCFF, valid_fcff(revenue_growth_y1=3.1), early_stage=True)
    v = check_bounds(ModelType.FCFF, valid_fcff(revenue_growth_y1=2.5))
    assert any("revenue_growth_y1" in m for m in v)
    v = check_bounds(ModelType.FCFE, valid_fcfe(revenue_growth_y2=2.0), early_stage=True)
    assert v == []


# --------------------------------------------------------------------------- cross-field rules


@pytest.mark.parametrize("factory", [valid_fcff, valid_fcfe])
def test_terminal_growth_le_risk_free(factory: Factory) -> None:
    mt = ModelType.FCFF if factory is valid_fcff else ModelType.FCFE
    assert check_bounds(mt, factory(terminal_growth_rate=0.042)) == []  # == rf OK
    v = check_bounds(mt, factory(terminal_growth_rate=0.045))
    assert any("must be <= risk_free_rate" in m for m in v)


@pytest.mark.parametrize("factory", [valid_fcff, valid_fcfe])
def test_terminal_growth_floor(factory: Factory) -> None:
    mt = ModelType.FCFF if factory is valid_fcff else ModelType.FCFE
    assert check_bounds(mt, factory(terminal_growth_rate=-0.02)) == []
    v = check_bounds(mt, factory(terminal_growth_rate=-0.03))
    assert any("terminal_growth_rate" in m and "out of bounds" in m for m in v)


def test_wacc_must_exceed_terminal_growth() -> None:
    # Low WACC: rf 3%, beta 0.3, ERP 2%, all-debt-ish capital at cheap after-tax cost.
    p = valid_fcff(
        risk_free_rate=0.03,
        levered_beta=0.3,
        equity_risk_premium=0.02,
        pretax_cost_of_debt=0.03,
        target_debt_to_capital=0.9,
        tax_rate=0.4,
        terminal_growth_rate=0.028,
    )
    assert fcff_wacc(p) < 0.028 + 0.005
    v = check_bounds(ModelType.FCFF, p)
    assert any("WACC" in m for m in v)
    ok = valid_fcff(terminal_growth_rate=0.03)
    assert fcff_wacc(ok) > 0.035
    assert check_bounds(ModelType.FCFF, ok) == []


def test_fcff_wacc_formula() -> None:
    p = valid_fcff()
    ke = 0.042 + 1.1 * 0.045
    assert fcff_wacc(p) == pytest.approx(ke * 0.8 + 0.055 * 0.79 * 0.2)


def test_fcfe_cost_of_equity_must_exceed_terminal_growth() -> None:
    p = valid_fcfe(risk_free_rate=0.03, levered_beta=0.3, equity_risk_premium=0.02, terminal_growth_rate=0.03)
    assert check_bounds(ModelType.FCFE, p) == []  # ke = 0.036 > 0.03 + 0.005
    # Within-range inputs can't breach it (ke >= rf + 0.006 > g + 0.005); a zero beta does.
    p = valid_fcfe(risk_free_rate=0.03, levered_beta=0.0, equity_risk_premium=0.02, terminal_growth_rate=0.03)
    v = check_bounds(ModelType.FCFE, p)
    assert any("cost of equity" in m for m in v)


def test_terminal_roic_must_exceed_positive_growth() -> None:
    v = check_bounds(ModelType.FCFF, valid_fcff(terminal_roic=0.02, terminal_growth_rate=0.025))
    assert any("terminal_roic" in m and "reinvestment" in m for m in v)
    assert check_bounds(ModelType.FCFF, valid_fcff(terminal_roic=0.02, terminal_growth_rate=0.0)) == []


def test_survival_flag_for_non_early_stage() -> None:
    v = check_bounds(ModelType.FCFF, valid_fcff(survival_probability=0.7))
    assert any("survival_probability" in m and "early-stage" in m for m in v)
    assert check_bounds(ModelType.FCFF, valid_fcff(survival_probability=0.8)) == []
    assert check_bounds(ModelType.FCFF, valid_fcff(survival_probability=0.7), early_stage=True) == []
    v = check_bounds(ModelType.FCFF, valid_fcff(survival_probability=0.0), early_stage=True)
    assert any("survival_probability" in m for m in v)


def test_excess_return_cost_of_equity_gt_terminal_growth() -> None:
    p = valid_excess_return(cost_of_equity=0.05, terminal_growth_rate=0.05)
    v = check_bounds(ModelType.EXCESS_RETURN, p)
    assert any("cost_of_equity" in m and "must exceed" in m for m in v)
    assert (
        check_bounds(
            ModelType.EXCESS_RETURN, valid_excess_return(cost_of_equity=0.06, terminal_growth_rate=0.05)
        )
        == []
    )
    # the engine needs ke - g >= 0.5pt; a thinner spread must be caught here, not at build time
    thin = check_bounds(
        ModelType.EXCESS_RETURN, valid_excess_return(cost_of_equity=0.054, terminal_growth_rate=0.05)
    )
    assert any("cost_of_equity" in m and "by more than 0.005" in m for m in thin)


def test_excess_return_terminal_growth_vs_supplied_rf() -> None:
    p = valid_excess_return(terminal_growth_rate=0.045)
    assert check_bounds(ModelType.EXCESS_RETURN, p) == []  # absolute cap 6% only
    v = check_bounds(ModelType.EXCESS_RETURN, p, risk_free_rate=0.04)
    assert any("must be <= risk_free_rate" in m for m in v)
    v = check_bounds(ModelType.EXCESS_RETURN, valid_excess_return(terminal_growth_rate=0.07))
    assert any("terminal_growth_rate" in m for m in v)


def test_excess_return_payout_bounds() -> None:
    # payout moves bv-growth consistency too, so adjust book growth alongside
    assert (
        check_bounds(
            ModelType.EXCESS_RETURN, valid_excess_return(payout_ratio=0.0, book_value_growth_rate=0.112)
        )
        == []
    )
    assert (
        check_bounds(
            ModelType.EXCESS_RETURN, valid_excess_return(payout_ratio=1.0, book_value_growth_rate=0.0)
        )
        == []
    )
    v = check_bounds(
        ModelType.EXCESS_RETURN, valid_excess_return(payout_ratio=1.1, book_value_growth_rate=-0.01)
    )
    assert any("payout_ratio" in m for m in v)
    v = check_bounds(
        ModelType.EXCESS_RETURN, valid_excess_return(payout_ratio=-0.1, book_value_growth_rate=0.12)
    )
    assert any("payout_ratio" in m for m in v)


def test_excess_return_book_value_growth_consistency() -> None:
    v = check_bounds(ModelType.EXCESS_RETURN, valid_excess_return(book_value_growth_rate=0.2))
    assert any("book_value_growth_rate" in m and "inconsistent" in m for m in v)
    v = check_bounds(ModelType.EXCESS_RETURN, valid_excess_return(book_value_growth_rate=0.6))
    assert any("book_value_growth_rate" in m and "out of bounds" in m for m in v)


def test_non_finite_values_flagged() -> None:
    v = check_bounds(ModelType.NAV_REIT, valid_reit(cap_rate=float("nan")))
    assert any("finite" in m for m in v)
    v = check_bounds(ModelType.FCFF, valid_fcff(levered_beta=float("inf")))
    assert any("finite" in m for m in v)


# --------------------------------------------------------------------------- dispatch + SOTP


def test_wrong_schema_for_model_type() -> None:
    v = check_bounds(ModelType.NAV_REIT, valid_fcff())
    assert v and "expected ReitNavAssumptions" in v[0]
    v = check_bounds(ModelType.FCFF, valid_segment_multiple())
    assert v and "only valid for sotp" in v[0]
    assert check_bounds("fcfe", valid_fcfe()) == []  # str model type accepted


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


def test_sotp_valid() -> None:
    p = _sotp([_fcff_seg("A"), _mult_seg("B"), _mult_seg("C", margin=None)], consolidated=valid_fcff())
    assert check_bounds(ModelType.SOTP, p) == []
    assert check_bounds(ModelType.SOTP, _sotp([_fcff_seg("A")], overhead=0.0)) == []


def test_sotp_recurses_into_segment_fcff() -> None:
    p = _sotp([_fcff_seg("A", valid_fcff(terminal_growth_rate=0.08)), _mult_seg("B")])
    v = check_bounds(ModelType.SOTP, p)
    assert v and all(m.startswith("segment[A].") for m in v)


def test_sotp_recurses_into_consolidated() -> None:
    v = check_bounds(ModelType.SOTP, _sotp([_mult_seg("B")], consolidated=valid_fcff(tax_rate=0.7)))
    assert any(m.startswith("consolidated_fcff.tax_rate") for m in v)


@pytest.mark.parametrize("multiple", [0.0, -3.0, 40.5, float("inf")])
def test_sotp_multiple_bounds(multiple: float) -> None:
    v = check_bounds(ModelType.SOTP, _sotp([_mult_seg("B", multiple=multiple)]))
    assert any("segment[B].ev_ebitda_multiple" in m for m in v)


def test_sotp_multiple_upper_edge_ok() -> None:
    assert check_bounds(ModelType.SOTP, _sotp([_mult_seg("B", multiple=40.0)])) == []


def test_sotp_segment_margin_bounds() -> None:
    v = check_bounds(ModelType.SOTP, _sotp([_mult_seg("B", margin=0.95)]))
    assert any("segment_ebitda_margin" in m for m in v)


def test_sotp_structural_rules() -> None:
    assert "segments must not be empty" in check_bounds(ModelType.SOTP, _sotp([]))
    v = check_bounds(ModelType.SOTP, _sotp([_mult_seg("B"), _mult_seg("B")]))
    assert any("unique" in m for m in v)
    missing = SotpSegmentAssumption(segment_name="A", valuation_approach="fcff", ev_ebitda_multiple=0)
    v = check_bounds(ModelType.SOTP, _sotp([missing]))
    assert any("fcff_assumptions required" in m for m in v)
    weird = SotpSegmentAssumption(segment_name="A", valuation_approach="dcf", ev_ebitda_multiple=5)
    v = check_bounds(ModelType.SOTP, _sotp([weird]))
    assert any("valuation_approach" in m for m in v)
    v = check_bounds(ModelType.SOTP, _sotp([_mult_seg("B")], overhead=5e8))
    assert any("corporate_overhead_capitalized" in m for m in v)


# --------------------------------------------------------------------------- composable pieces + prompt text


def test_small_checks_compose() -> None:
    rule = bounds.RangeRule("x", 0.0, 1.0, lo_open=True)
    assert bounds.check_range(0.5, rule) == []
    assert bounds.check_range(0.0, rule) and bounds.check_range(1.5, rule)
    assert "x > 0 and <= 1" == rule.describe()
    assert bounds.check_terminal_growth_vs_rf(0.03, 0.04) == []
    assert bounds.check_terminal_growth_vs_rf(0.05, 0.04)
    assert bounds.check_discount_rate_exceeds_growth(0.08, 0.03, "WACC") == []
    assert bounds.check_discount_rate_exceeds_growth(0.034, 0.03, "WACC")
    assert bounds.check_survival(0.5, early_stage=True) == []
    assert bounds.check_survival(0.5, early_stage=False)


@pytest.mark.parametrize("schema_cls", [FCFFAssumptions, FCFEAssumptions])
def test_describe_bounds_mentions_every_ranged_field(schema_cls: type[BaseModel]) -> None:
    text = "\n".join(describe_bounds(schema_cls))
    for name in schema_cls.model_fields:
        assert name in text, name
    assert "3" in "\n".join(describe_bounds(FCFFAssumptions, early_stage=True))


def test_describe_bounds_unknown_schema_is_empty() -> None:
    class NotAnAssumptionSchema(BaseModel):
        x: float = 0.0

    assert describe_bounds(NotAnAssumptionSchema) == []
