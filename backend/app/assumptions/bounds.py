"""Assumption sanity bounds (spec §5.6).

Every check is a small function returning ``list[str]`` of human-readable violations
(empty list == OK). The messages are fed verbatim back to Claude in the repair prompt,
so they name the field, the offending value and the allowed range.

Per-field ranges are table-driven (``RangeRule`` tuples per schema); cross-field rules
(terminal growth vs. risk-free, WACC vs. terminal growth, ...) are explicit functions.
``describe_bounds`` renders the same tables as prompt text so Claude sees the rules up
front and usually passes on the first attempt.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from pydantic import BaseModel

from app.schemas.assumptions import (
    ASSUMPTION_SCHEMA_BY_MODEL,
    AssumptionField,
    EpNavAssumptions,
    ExcessReturnAssumptions,
    FCFEAssumptions,
    FCFFAssumptions,
    ReitNavAssumptions,
    SegmentMultipleAssumptions,
    SotpAssumptions,
)
from app.schemas.company import ModelType

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GROWTH_FIELDS = tuple(f"revenue_growth_y{i}" for i in range(1, 6))
ROE_FIELDS = tuple(f"roe_y{i}" for i in range(1, 6))

GROWTH_MIN = -0.5
GROWTH_MAX = 1.5
GROWTH_MAX_EARLY_STAGE = 3.0
TERMINAL_GROWTH_MIN = -0.02
DISCOUNT_SPREAD_MIN = 0.005  # WACC / cost of equity must exceed terminal growth by at least this
NON_EARLY_STAGE_SURVIVAL_MIN = 0.8
EXCESS_RETURN_TERMINAL_GROWTH_MAX = 0.06  # absolute cap when no risk-free rate is supplied
BOOK_VALUE_GROWTH_TOLERANCE = 0.05  # |bv_growth - avg ROE * (1 - payout)|
SOTP_MULTIPLE_MAX = 40.0
FCFF_APPROACH = "fcff"
MULTIPLE_APPROACH = "ev_ebitda_multiple"


@dataclass(frozen=True)
class RangeRule:
    """``lo <= value <= hi`` (bounds optional; ``*_open`` makes them strict)."""

    field: str
    lo: float | None = None
    hi: float | None = None
    lo_open: bool = False
    hi_open: bool = False

    def describe(self) -> str:
        parts = []
        if self.lo is not None:
            parts.append(f"{'>' if self.lo_open else '>='} {self.lo:g}")
        if self.hi is not None:
            parts.append(f"{'<' if self.hi_open else '<='} {self.hi:g}")
        return f"{self.field} {' and '.join(parts)}"


def _growth_rules(early_stage: bool) -> tuple[RangeRule, ...]:
    hi = GROWTH_MAX_EARLY_STAGE if early_stage else GROWTH_MAX
    return tuple(RangeRule(f, GROWTH_MIN, hi) for f in GROWTH_FIELDS)


_RF = RangeRule("risk_free_rate", 0.0, 0.10)
_ERP = RangeRule("equity_risk_premium", 0.02, 0.10)
_BETA = RangeRule("levered_beta", 0.3, 3.5)
_TAX = RangeRule("tax_rate", 0.0, 0.50)
_DEBT_CAP = RangeRule("target_debt_to_capital", 0.0, 0.9)
_CONVERGENCE = RangeRule("margin_convergence_years", 1.0, 10.0)
_TERMINAL_G_FLOOR = RangeRule("terminal_growth_rate", TERMINAL_GROWTH_MIN, None)

FCFF_RULES: tuple[RangeRule, ...] = (
    RangeRule("target_operating_margin", -1.0, 0.8),
    _CONVERGENCE,
    _TAX,
    RangeRule("sales_to_capital_ratio", 0.0, 20.0, lo_open=True),
    _RF,
    _ERP,
    _BETA,
    RangeRule("pretax_cost_of_debt", 0.0, 0.25),
    _DEBT_CAP,
    _TERMINAL_G_FLOOR,
    RangeRule("terminal_roic", 0.0, 1.0, lo_open=True),
    RangeRule("survival_probability", 0.0, 1.0, lo_open=True),
)

FCFE_RULES: tuple[RangeRule, ...] = (
    RangeRule("target_net_margin", -1.0, 0.8),
    _CONVERGENCE,
    _TAX,
    _DEBT_CAP,
    RangeRule("net_borrowing_as_pct_reinvestment", 0.0, 1.0),
    _RF,
    _ERP,
    _BETA,
    _TERMINAL_G_FLOOR,
)

EXCESS_RETURN_RULES: tuple[RangeRule, ...] = (
    *(RangeRule(f, -0.3, 0.5) for f in ROE_FIELDS),
    RangeRule("terminal_roe", -0.3, 0.5),
    RangeRule("cost_of_equity", 0.05, 0.20),
    RangeRule("book_value_growth_rate", -0.3, 0.5),
    RangeRule("payout_ratio", 0.0, 1.0),
    RangeRule("terminal_growth_rate", TERMINAL_GROWTH_MIN, EXCESS_RETURN_TERMINAL_GROWTH_MAX),
)

REIT_NAV_RULES: tuple[RangeRule, ...] = (
    RangeRule("cap_rate", 0.03, 0.12),
    RangeRule("noi_growth_rate", -0.10, 0.15),
    RangeRule("non_real_estate_asset_adjustment", 0.0, None),
    RangeRule("liability_adjustment", 0.0, None),
)

EP_NAV_RULES: tuple[RangeRule, ...] = (
    RangeRule("price_deck_oil_per_bbl", 20.0, 200.0),
    RangeRule("price_deck_gas_per_mcf", 0.5, 20.0),
    RangeRule("discount_rate_pv10", 0.05, 0.20),
    RangeRule("development_cost_adjustment", 0.0, None),
)

SEGMENT_MULTIPLE_RULES: tuple[RangeRule, ...] = (
    RangeRule("ev_ebitda_multiple", 0.0, SOTP_MULTIPLE_MAX, lo_open=True),
    RangeRule("segment_ebitda_margin", -1.0, 0.8),
)


# ---------------------------------------------------------------------------
# Small composable checks
# ---------------------------------------------------------------------------


def _val(proposal: BaseModel, field: str) -> float:
    return float(getattr(proposal, field).value)


def _fmt(x: float) -> str:
    return f"{x:.4g}"


def check_finite(proposal: BaseModel, prefix: str = "") -> list[str]:
    """Every AssumptionField value must be a finite number."""
    out = []
    for name in type(proposal).model_fields:
        f = getattr(proposal, name)
        if isinstance(f, AssumptionField) and not math.isfinite(f.value):
            out.append(f"{prefix}{name}={f.value} must be a finite number")
    return out


def check_range(value: float, rule: RangeRule, prefix: str = "") -> list[str]:
    if not math.isfinite(value):
        return []  # reported by check_finite
    too_low = rule.lo is not None and (value <= rule.lo if rule.lo_open else value < rule.lo)
    too_high = rule.hi is not None and (value >= rule.hi if rule.hi_open else value > rule.hi)
    if too_low or too_high:
        return [f"{prefix}{rule.field}={_fmt(value)} out of bounds: must be {rule.describe()}"]
    return []


def check_ranges(proposal: BaseModel, rules: Iterable[RangeRule], prefix: str = "") -> list[str]:
    out: list[str] = []
    for rule in rules:
        out += check_range(_val(proposal, rule.field), rule, prefix)
    return out


def check_terminal_growth_vs_rf(terminal_growth: float, risk_free: float, prefix: str = "") -> list[str]:
    if terminal_growth > risk_free:
        return [
            f"{prefix}terminal_growth_rate={_fmt(terminal_growth)} must be <= risk_free_rate={_fmt(risk_free)}"
            " (no firm grows faster than the economy forever)"
        ]
    return []


def check_discount_rate_exceeds_growth(
    rate: float, terminal_growth: float, label: str, prefix: str = "", spread: float = DISCOUNT_SPREAD_MIN
) -> list[str]:
    if rate <= terminal_growth + spread:
        return [
            f"{prefix}{label}={_fmt(rate)} must exceed terminal_growth_rate={_fmt(terminal_growth)}"
            f" by more than {spread:g} (terminal value blows up otherwise)"
        ]
    return []


def check_terminal_roic(terminal_roic: float, terminal_growth: float, prefix: str = "") -> list[str]:
    """Terminal reinvestment rate = g / ROIC must be < 100% when g > 0."""
    if terminal_growth > 0 and terminal_roic <= terminal_growth:
        return [
            f"{prefix}terminal_roic={_fmt(terminal_roic)} must exceed terminal_growth_rate="
            f"{_fmt(terminal_growth)} (terminal reinvestment rate g/ROIC would be >= 100%)"
        ]
    return []


def check_survival(survival: float, early_stage: bool, prefix: str = "") -> list[str]:
    if not early_stage and 0 < survival < NON_EARLY_STAGE_SURVIVAL_MIN:
        return [
            f"{prefix}survival_probability={_fmt(survival)} is below {NON_EARLY_STAGE_SURVIVAL_MIN} for a"
            " non-early-stage firm; use 1.0 for a stable company"
        ]
    return []


def check_book_value_growth_consistency(p: ExcessReturnAssumptions, prefix: str = "") -> list[str]:
    avg_roe = sum(_val(p, f) for f in ROE_FIELDS) / len(ROE_FIELDS)
    implied = avg_roe * (1 - p.payout_ratio.value)
    bvg = p.book_value_growth_rate.value
    if abs(bvg - implied) > BOOK_VALUE_GROWTH_TOLERANCE:
        return [
            f"{prefix}book_value_growth_rate={_fmt(bvg)} inconsistent with average ROE x (1 - payout_ratio)"
            f"={_fmt(implied)} (allowed gap {BOOK_VALUE_GROWTH_TOLERANCE:g})"
        ]
    return []


def fcff_wacc(p: FCFFAssumptions) -> float:
    """Same formula as the engine (§6.2): ke*(1-d) + kd*(1-t)*d."""
    ke = p.risk_free_rate.value + p.levered_beta.value * p.equity_risk_premium.value
    kd = p.pretax_cost_of_debt.value * (1 - p.tax_rate.value)
    d = p.target_debt_to_capital.value
    return ke * (1 - d) + kd * d


def capm_cost_of_equity(p: FCFFAssumptions | FCFEAssumptions) -> float:
    return p.risk_free_rate.value + p.levered_beta.value * p.equity_risk_premium.value


# ---------------------------------------------------------------------------
# Per-schema checks
# ---------------------------------------------------------------------------


def check_fcff(p: FCFFAssumptions, *, early_stage: bool = False, prefix: str = "") -> list[str]:
    out = check_finite(p, prefix)
    out += check_ranges(p, _growth_rules(early_stage) + FCFF_RULES, prefix)
    g = p.terminal_growth_rate.value
    out += check_terminal_growth_vs_rf(g, p.risk_free_rate.value, prefix)
    out += check_discount_rate_exceeds_growth(fcff_wacc(p), g, "WACC (derived)", prefix)
    out += check_terminal_roic(p.terminal_roic.value, g, prefix)
    out += check_survival(p.survival_probability.value, early_stage, prefix)
    return out


def check_fcfe(p: FCFEAssumptions, *, early_stage: bool = False, prefix: str = "") -> list[str]:
    out = check_finite(p, prefix)
    out += check_ranges(p, _growth_rules(early_stage) + FCFE_RULES, prefix)
    g = p.terminal_growth_rate.value
    out += check_terminal_growth_vs_rf(g, p.risk_free_rate.value, prefix)
    out += check_discount_rate_exceeds_growth(capm_cost_of_equity(p), g, "cost of equity (derived)", prefix)
    return out


def check_excess_return(
    p: ExcessReturnAssumptions, *, risk_free_rate: float | None = None, prefix: str = ""
) -> list[str]:
    out = check_finite(p, prefix)
    out += check_ranges(p, EXCESS_RETURN_RULES, prefix)
    g = p.terminal_growth_rate.value
    out += check_discount_rate_exceeds_growth(p.cost_of_equity.value, g, "cost_of_equity", prefix, spread=0.0)
    if risk_free_rate is not None:
        out += check_terminal_growth_vs_rf(g, risk_free_rate, prefix)
    out += check_book_value_growth_consistency(p, prefix)
    return out


def check_reit_nav(p: ReitNavAssumptions, *, prefix: str = "") -> list[str]:
    return check_finite(p, prefix) + check_ranges(p, REIT_NAV_RULES, prefix)


def check_ep_nav(p: EpNavAssumptions, *, prefix: str = "") -> list[str]:
    return check_finite(p, prefix) + check_ranges(p, EP_NAV_RULES, prefix)


def check_segment_multiple(p: SegmentMultipleAssumptions, *, prefix: str = "") -> list[str]:
    return check_finite(p, prefix) + check_ranges(p, SEGMENT_MULTIPLE_RULES, prefix)


def check_sotp(p: SotpAssumptions) -> list[str]:
    out: list[str] = []
    if not p.segments:
        out.append("segments must not be empty")
    names = [s.segment_name for s in p.segments]
    if len(set(names)) != len(names):
        out.append(f"segment names must be unique, got {names}")
    for seg in p.segments:
        prefix = f"segment[{seg.segment_name}]."
        if seg.valuation_approach == FCFF_APPROACH:
            if seg.fcff_assumptions is None:
                out.append(f"{prefix}fcff_assumptions required when valuation_approach='fcff'")
            else:
                out += check_fcff(seg.fcff_assumptions, prefix=prefix)
        elif seg.valuation_approach == MULTIPLE_APPROACH:
            rule = SEGMENT_MULTIPLE_RULES[0]
            out += check_range(seg.ev_ebitda_multiple, rule, prefix)
            if not math.isfinite(seg.ev_ebitda_multiple):
                out.append(f"{prefix}ev_ebitda_multiple must be a finite number")
            if seg.segment_ebitda_margin is not None:
                out += check_range(seg.segment_ebitda_margin, SEGMENT_MULTIPLE_RULES[1], prefix)
        else:
            out.append(
                f"{prefix}valuation_approach={seg.valuation_approach!r} must be "
                f"'{FCFF_APPROACH}' or '{MULTIPLE_APPROACH}'"
            )
    overhead = p.corporate_overhead_capitalized.value
    if not math.isfinite(overhead) or overhead > 0:
        out.append(
            f"corporate_overhead_capitalized={_fmt(overhead)} must be <= 0 (it is a negative EV adjustment)"
        )
    if p.consolidated_fcff is not None:
        out += check_fcff(p.consolidated_fcff, prefix="consolidated_fcff.")
    return out


# ---------------------------------------------------------------------------
# Dispatcher + prompt text
# ---------------------------------------------------------------------------


def check_bounds(
    model_type: ModelType | str,
    proposal: BaseModel,
    *,
    early_stage: bool = False,
    risk_free_rate: float | None = None,
) -> list[str]:
    """All violations for ``proposal`` (empty list == passes).

    ``risk_free_rate`` (optional) lets the excess-return check cap terminal growth at
    the market risk-free rate — that schema has no rf field of its own. A
    ``SegmentMultipleAssumptions`` proposal is accepted under ``ModelType.SOTP``.
    """
    mt = ModelType(model_type)
    if isinstance(proposal, SegmentMultipleAssumptions):
        if mt is not ModelType.SOTP:
            return [f"SegmentMultipleAssumptions is only valid for sotp, not {mt.value}"]
        return check_segment_multiple(proposal)
    if isinstance(proposal, FCFFAssumptions) and mt is ModelType.SOTP:
        return check_fcff(proposal, early_stage=early_stage)  # a per-segment / consolidated FCFF call
    expected = ASSUMPTION_SCHEMA_BY_MODEL[mt]
    if not isinstance(proposal, expected):
        return [f"expected {expected.__name__} for {mt.value}, got {type(proposal).__name__}"]
    match proposal:
        case FCFFAssumptions():
            return check_fcff(proposal, early_stage=early_stage)
        case FCFEAssumptions():
            return check_fcfe(proposal, early_stage=early_stage)
        case ExcessReturnAssumptions():
            return check_excess_return(proposal, risk_free_rate=risk_free_rate)
        case ReitNavAssumptions():
            return check_reit_nav(proposal)
        case EpNavAssumptions():
            return check_ep_nav(proposal)
        case SotpAssumptions():
            return check_sotp(proposal)
    return []  # pragma: no cover


def describe_bounds(schema_cls: type[BaseModel], *, early_stage: bool = False) -> list[str]:
    """Human-readable rules for ``schema_cls`` — included in the proposal prompt."""
    growth = [r.describe() for r in _growth_rules(early_stage)]
    if schema_cls is FCFFAssumptions:
        return [
            *growth,
            *(r.describe() for r in FCFF_RULES),
            "terminal_growth_rate <= risk_free_rate",
            f"WACC = (rf + beta*ERP)*(1-D/C) + pretax_cost_of_debt*(1-tax)*D/C must exceed "
            f"terminal_growth_rate + {DISCOUNT_SPREAD_MIN}",
            "terminal_roic > terminal_growth_rate (terminal reinvestment rate = g / ROIC)",
            "survival_probability = 1.0 for stable firms"
            + ("" if not early_stage else "; < 1.0 allowed for this early-stage firm"),
        ]
    if schema_cls is FCFEAssumptions:
        return [
            *growth,
            *(r.describe() for r in FCFE_RULES),
            "terminal_growth_rate <= risk_free_rate",
            f"cost of equity = rf + beta*ERP must exceed terminal_growth_rate + {DISCOUNT_SPREAD_MIN}",
        ]
    if schema_cls is ExcessReturnAssumptions:
        return [
            *(r.describe() for r in EXCESS_RETURN_RULES),
            "cost_of_equity > terminal_growth_rate",
            "terminal_growth_rate <= the market risk-free rate",
            f"book_value_growth_rate within {BOOK_VALUE_GROWTH_TOLERANCE} of "
            "average(roe_y1..roe_y5) * (1 - payout_ratio)",
        ]
    if schema_cls is ReitNavAssumptions:
        return [r.describe() for r in REIT_NAV_RULES]
    if schema_cls is EpNavAssumptions:
        return [r.describe() for r in EP_NAV_RULES]
    if schema_cls is SegmentMultipleAssumptions:
        return [r.describe() for r in SEGMENT_MULTIPLE_RULES]
    return []
