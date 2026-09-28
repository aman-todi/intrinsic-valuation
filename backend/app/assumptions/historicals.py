"""Historical medians / trends computed from ``NormalizedFinancials`` (spec §5.6).

These are the anchors placed in the proposal prompt and the inputs of the
deterministic fallback. Everything is best-effort: any metric that cannot be computed
from the available data is ``None`` and simply omitted from the prompt.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from app.schemas.company import ModelType
from app.schemas.financials import (
    BalanceSheetLine,
    CashFlowLine,
    IncomeStatementLine,
    MarketSnapshot,
    NormalizedFinancials,
    SegmentLine,
)


class Kind(StrEnum):
    PCT = "pct"  # decimal rendered as a percentage
    RATIO = "ratio"  # plain multiple, e.g. 1.8x
    USD = "usd"
    NUMBER = "number"
    PRICE = "price"


@dataclass(frozen=True)
class Anchor:
    key: str
    label: str
    value: float
    kind: Kind

    def render(self) -> str:
        return f"- {self.label} [{self.key}]: {format_value(self.value, self.kind)}"


def format_value(value: float, kind: Kind) -> str:
    match kind:
        case Kind.PCT:
            return f"{value:.4f} ({value * 100:.2f}%)"
        case Kind.RATIO:
            return f"{value:.2f}x"
        case Kind.USD:
            return f"${value:,.0f} ({format_usd_short(value)})"
        case Kind.PRICE:
            return f"${value:,.2f}"
        case _:
            return f"{value:,.4g}"


def format_usd_short(value: float) -> str:
    a = abs(value)
    sign = "-" if value < 0 else ""
    for div, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= div:
            return f"{sign}${a / div:.2f}{suffix}"
    return f"{sign}${a:.0f}"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _median(xs: Sequence[float | None]) -> float | None:
    vals = [x for x in xs if x is not None and math.isfinite(x)]
    return statistics.median(vals) if vals else None


def _div(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0 or not math.isfinite(a) or not math.isfinite(b):
        return None
    return a / b


def _cagr(first: float, last: float, years: int) -> float | None:
    if years <= 0 or first <= 0 or last <= 0:
        return None
    return (last / first) ** (1 / years) - 1


def annual_rows[T: (IncomeStatementLine, BalanceSheetLine, CashFlowLine)](
    rows: list[T], window_years: int
) -> list[T]:
    """Last ``window_years`` non-TTM rows, oldest -> newest."""
    return [r for r in rows if not r.period.is_ttm][-window_years:]


def latest_segment_rows(financials: NormalizedFinancials) -> dict[str, SegmentLine]:
    """Most recent row per segment name."""
    out: dict[str, SegmentLine] = {}
    for s in sorted(financials.segments, key=lambda s: (s.period.fiscal_year, s.period.is_ttm)):
        out[s.segment_name] = s
    return out


def segment_names(financials: NormalizedFinancials) -> list[str]:
    """Segment names reported in the most recent fiscal year, in first-seen order."""
    if not financials.segments:
        return []
    latest_year = max(s.period.fiscal_year for s in financials.segments)
    names: list[str] = []
    for s in financials.segments:
        if s.period.fiscal_year == latest_year and s.segment_name not in names:
            names.append(s.segment_name)
    return names


class Anchors(dict[str, Anchor]):
    """Ordered ``key -> Anchor`` mapping with a ``get_value`` convenience."""

    def add(self, key: str, label: str, value: float | None, kind: Kind) -> None:
        if value is not None and math.isfinite(value):
            self[key] = Anchor(key, label, float(value), kind)

    def v(self, key: str) -> float | None:
        a = self.get(key)
        return a.value if a else None

    def render(self) -> str:
        return "\n".join(a.render() for a in self.values()) or "- (no usable history)"


# ---------------------------------------------------------------------------
# anchor computation
# ---------------------------------------------------------------------------


def compute_anchors(
    model_type: ModelType | str,
    financials: NormalizedFinancials,
    market: MarketSnapshot,
    *,
    window_years: int = 5,
) -> Anchors:
    mt = ModelType(model_type)
    a = Anchors()
    _core_anchors(a, financials, market, window_years)
    if mt is ModelType.EXCESS_RETURN:
        _bank_insurer_anchors(a, financials, window_years)
    elif mt is ModelType.NAV_REIT:
        _reit_anchors(a, financials, window_years)
    elif mt is ModelType.NAV_EP:
        _ep_anchors(a, financials, window_years)
    return a


def _core_anchors(a: Anchors, f: NormalizedFinancials, market: MarketSnapshot, window_years: int) -> None:
    inc = annual_rows(f.income_statements, window_years)
    bs_by_year = {b.period.fiscal_year: b for b in f.balance_sheets if not b.period.is_ttm}
    cfs = annual_rows(f.cash_flows, window_years)
    latest_is = f.income_statements[-1] if f.income_statements else None
    latest_bs = f.balance_sheets[-1] if f.balance_sheets else None

    a.add("years_of_history", "Fiscal years in window", len(inc), Kind.NUMBER)
    if len(inc) >= 2:
        a.add(
            "revenue_cagr",
            f"Revenue CAGR FY{inc[0].period.fiscal_year}-FY{inc[-1].period.fiscal_year}",
            _cagr(inc[0].revenue, inc[-1].revenue, len(inc) - 1),
            Kind.PCT,
        )
        growths = [
            g
            for p, c in zip(inc, inc[1:], strict=False)
            if p.revenue > 0 and (g := _div(c.revenue - p.revenue, p.revenue)) is not None
        ]
        if growths:
            a.add("revenue_growth_latest", "Latest annual revenue growth", growths[-1], Kind.PCT)
            a.add("revenue_growth_median", "Median annual revenue growth", _median(growths), Kind.PCT)
    if inc:
        a.add("revenue_latest_fy", f"Revenue FY{inc[-1].period.fiscal_year}", inc[-1].revenue, Kind.USD)
    if latest_is is not None and latest_is.period.is_ttm:
        a.add("revenue_ttm", "Revenue TTM", latest_is.revenue, Kind.USD)

    op_margins = [_div(r.operating_income, r.revenue) for r in inc if r.revenue > 0]
    a.add("operating_margin_median", "Median operating margin", _median(op_margins), Kind.PCT)
    if latest_is is not None and latest_is.revenue > 0:
        a.add(
            "operating_margin_latest",
            "Latest operating margin",
            _div(latest_is.operating_income, latest_is.revenue),
            Kind.PCT,
        )
    if len(op_margins) >= 2 and all(m is not None for m in op_margins):
        a.add("operating_margin_min", "Min operating margin in window", min(op_margins), Kind.PCT)  # type: ignore[type-var]
        a.add("operating_margin_max", "Max operating margin in window", max(op_margins), Kind.PCT)  # type: ignore[type-var]
    net_margins = [_div(r.net_income, r.revenue) for r in inc if r.revenue > 0]
    a.add("net_margin_median", "Median net margin", _median(net_margins), Kind.PCT)
    tax_rates = [_div(r.tax_expense, r.pretax_income) for r in inc if r.pretax_income > 0]
    a.add("effective_tax_rate_median", "Median effective tax rate", _median(tax_rates), Kind.PCT)

    # Reinvestment intensity
    rev_by_year = {r.period.fiscal_year: r.revenue for r in inc}
    capex_int = [_div(abs(c.capex), rev_by_year.get(c.period.fiscal_year)) for c in cfs]
    da_int = [_div(c.depreciation_amortization, rev_by_year.get(c.period.fiscal_year)) for c in cfs]
    capex_da = [
        _div(abs(c.capex), c.depreciation_amortization) for c in cfs if c.depreciation_amortization > 0
    ]
    a.add("capex_to_revenue_median", "Median capex / revenue", _median(capex_int), Kind.PCT)
    a.add("da_to_revenue_median", "Median D&A / revenue", _median(da_int), Kind.PCT)
    a.add("capex_to_da_median", "Median capex / D&A", _median(capex_da), Kind.RATIO)
    sbc_int = [_div(c.stock_based_comp, rev_by_year.get(c.period.fiscal_year)) for c in cfs]
    a.add("sbc_to_revenue_median", "Median stock-based comp / revenue", _median(sbc_int), Kind.PCT)
    if len(inc) >= 2 and cfs:
        d_rev = inc[-1].revenue - inc[0].revenue
        reinvest = sum(abs(c.capex) - c.depreciation_amortization + c.change_in_nwc for c in cfs[1:])
        if reinvest > 0 and d_rev > 0:
            a.add(
                "sales_to_capital_historical",
                "Historical sales-to-capital (Δrevenue / net reinvestment)",
                d_rev / reinvest,
                Kind.RATIO,
            )

    # Capital structure
    if latest_bs is not None:
        invested = latest_bs.total_debt + latest_bs.total_equity - latest_bs.cash_and_equivalents
        if latest_is is not None and invested > 0:
            a.add(
                "revenue_to_invested_capital",
                "Revenue / invested capital (latest)",
                latest_is.revenue / invested,
                Kind.RATIO,
            )
            tax = a.v("effective_tax_rate_median") or 0.21
            a.add(
                "roic_latest",
                "After-tax ROIC (latest)",
                latest_is.operating_income * (1 - min(max(tax, 0), 0.5)) / invested,
                Kind.PCT,
            )
        a.add("total_debt", "Total debt (latest)", latest_bs.total_debt, Kind.USD)
        a.add("cash_and_equivalents", "Cash & equivalents (latest)", latest_bs.cash_and_equivalents, Kind.USD)
        a.add(
            "short_term_investments",
            "Short-term investments (latest)",
            latest_bs.short_term_investments,
            Kind.USD,
        )
        a.add("total_equity", "Book equity (latest)", latest_bs.total_equity, Kind.USD)
        a.add(
            "book_debt_to_equity",
            "Book debt / equity",
            _div(latest_bs.total_debt, latest_bs.total_equity) if latest_bs.total_equity > 0 else None,
            Kind.RATIO,
        )
        mcap = market.market_cap
        if mcap > 0:
            a.add(
                "market_debt_to_capital",
                "Debt / (debt + market cap)",
                latest_bs.total_debt / (latest_bs.total_debt + mcap),
                Kind.PCT,
            )
        if latest_is is not None and latest_bs.total_debt > 0:
            a.add(
                "implied_pretax_cost_of_debt",
                "Interest expense / total debt",
                abs(latest_is.interest_expense) / latest_bs.total_debt,
                Kind.PCT,
            )
        if latest_is is not None and latest_is.interest_expense:
            a.add(
                "interest_coverage",
                "Operating income / interest expense",
                latest_is.operating_income / abs(latest_is.interest_expense),
                Kind.RATIO,
            )

    # ROE history (all models; central for banks)
    roes = []
    for r in inc:
        prev = bs_by_year.get(r.period.fiscal_year - 1)
        cur = bs_by_year.get(r.period.fiscal_year)
        base = prev or cur
        if base is not None and base.total_equity > 0:
            roes.append(r.net_income / base.total_equity)
    if roes:
        a.add("roe_median", "Median ROE (net income / opening book equity)", _median(roes), Kind.PCT)
        a.add("roe_latest", "Latest ROE", roes[-1], Kind.PCT)
    bs_rows = [b for b in f.balance_sheets if not b.period.is_ttm][-window_years:]
    if len(bs_rows) >= 2:
        a.add(
            "book_value_cagr",
            "Book equity CAGR",
            _cagr(bs_rows[0].total_equity, bs_rows[-1].total_equity, len(bs_rows) - 1),
            Kind.PCT,
        )
        retention = []
        for r in inc:
            prev, cur = bs_by_year.get(r.period.fiscal_year - 1), bs_by_year.get(r.period.fiscal_year)
            if prev is not None and cur is not None and r.net_income > 0:
                retention.append((cur.total_equity - prev.total_equity) / r.net_income)
        med = _median(retention)
        if med is not None:
            a.add(
                "implied_payout_ratio",
                "Implied payout (1 - Δbook equity / net income), median",
                1 - med,
                Kind.PCT,
            )


def _bank_insurer_anchors(a: Anchors, f: NormalizedFinancials, window_years: int) -> None:
    bank = f.bank_data[-window_years:]
    if bank:
        last = bank[-1]
        a.add("net_interest_income", "Net interest income (latest)", last.net_interest_income, Kind.USD)
        a.add(
            "provision_to_nii",
            "Provision for credit losses / NII (median)",
            _median([_div(b.provision_for_credit_losses, b.net_interest_income) for b in bank]),
            Kind.PCT,
        )
        a.add("total_deposits", "Total deposits (latest)", last.total_deposits, Kind.USD)
        a.add("tangible_book_value", "Tangible book value (latest)", last.tangible_book_value, Kind.USD)
        if last.tier1_capital_ratio is not None:
            a.add("tier1_capital_ratio", "Tier 1 capital ratio (latest)", last.tier1_capital_ratio, Kind.PCT)
        if len(bank) >= 2:
            a.add(
                "nii_cagr",
                "NII CAGR",
                _cagr(bank[0].net_interest_income, last.net_interest_income, len(bank) - 1),
                Kind.PCT,
            )
    ins = f.insurer_data[-window_years:]
    if ins:
        a.add(
            "combined_ratio_median",
            "Median combined ratio",
            _median([i.combined_ratio for i in ins if i.combined_ratio is not None]),
            Kind.PCT,
        )
        a.add(
            "loss_ratio_median",
            "Median loss & LAE ratio",
            _median([i.loss_and_lae_ratio for i in ins if i.loss_and_lae_ratio is not None]),
            Kind.PCT,
        )
        a.add("net_premiums_earned", "Net premiums earned (latest)", ins[-1].net_premiums_earned, Kind.USD)
        a.add(
            "book_value_per_share", "Book value per share (latest)", ins[-1].book_value_per_share, Kind.PRICE
        )
        if len(ins) >= 2:
            a.add(
                "bvps_cagr",
                "Book value per share CAGR",
                _cagr(ins[0].book_value_per_share, ins[-1].book_value_per_share, len(ins) - 1),
                Kind.PCT,
            )


def _reit_anchors(a: Anchors, f: NormalizedFinancials, window_years: int) -> None:
    latest_is = f.income_statements[-1] if f.income_statements else None
    latest_cf = f.cash_flows[-1] if f.cash_flows else None
    if latest_is is not None and latest_cf is not None:
        noi = latest_is.operating_income + latest_cf.depreciation_amortization
        a.add("noi_proxy", "NOI proxy (operating income + D&A, latest)", noi, Kind.USD)
    reit = f.reit_data[-window_years:]
    if reit:
        last = reit[-1]
        a.add(
            "real_estate_gross",
            "Real estate investments, gross (latest)",
            last.real_estate_investments_gross,
            Kind.USD,
        )
        a.add(
            "real_estate_net",
            "Real estate, net of accumulated depreciation",
            last.real_estate_investments_gross - last.accumulated_depreciation,
            Kind.USD,
        )
        noi_proxy = a.v("noi_proxy")
        if noi_proxy is not None and last.real_estate_investments_gross > 0:
            a.add(
                "implied_cap_rate_on_gross_book",
                "NOI proxy / gross real estate (book cap rate)",
                noi_proxy / last.real_estate_investments_gross,
                Kind.PCT,
            )
        if last.ffo is not None:
            a.add("ffo", "FFO (latest)", last.ffo, Kind.USD)
        if last.affo is not None:
            a.add("affo", "AFFO (latest)", last.affo, Kind.USD)
        ffos = [r.ffo for r in reit if r.ffo is not None]
        if len(ffos) >= 2:
            a.add("ffo_cagr", "FFO CAGR", _cagr(ffos[0], ffos[-1], len(ffos) - 1), Kind.PCT)
    inc = annual_rows(f.income_statements, window_years)
    noi_hist = []
    cf_by_year = {c.period.fiscal_year: c for c in f.cash_flows if not c.period.is_ttm}
    for r in inc:
        cf = cf_by_year.get(r.period.fiscal_year)
        if cf is not None:
            noi_hist.append(r.operating_income + cf.depreciation_amortization)
    if len(noi_hist) >= 2:
        a.add(
            "noi_proxy_cagr", "NOI proxy CAGR", _cagr(noi_hist[0], noi_hist[-1], len(noi_hist) - 1), Kind.PCT
        )
    if f.balance_sheets:
        bs = f.balance_sheets[-1]
        a.add(
            "non_real_estate_assets",
            "Cash + short-term investments (latest)",
            bs.cash_and_equivalents + bs.short_term_investments,
            Kind.USD,
        )
        a.add(
            "debt_plus_preferred",
            "Total debt + preferred + pension deficit (latest)",
            bs.total_debt + bs.preferred_equity + (bs.pension_deficit or 0.0),
            Kind.USD,
        )


def _ep_anchors(a: Anchors, f: NormalizedFinancials, window_years: int) -> None:
    ep = f.ep_data[-window_years:]
    if not ep:
        return
    last = ep[-1]
    sm = last.standardized_measure_disc_future_cash_flows
    if sm is not None:
        a.add("standardized_measure", "SEC standardized measure (PV-10-like, latest)", sm, Kind.USD)
    oil, gas = last.proved_reserves_oil_mmbbl, last.proved_reserves_gas_bcf
    if oil is not None:
        a.add("proved_oil_mmbbl", "Proved oil reserves (MMbbl)", oil, Kind.NUMBER)
    if gas is not None:
        a.add("proved_gas_bcf", "Proved gas reserves (Bcf)", gas, Kind.NUMBER)
    boe = (oil or 0.0) + (gas or 0.0) / 6.0  # MMboe
    if boe > 0:
        a.add("proved_reserves_mmboe", "Proved reserves (MMboe, 6 Mcf = 1 boe)", boe, Kind.NUMBER)
        if oil is not None:
            a.add("oil_weighting", "Oil share of reserves (boe basis)", (oil or 0.0) / boe, Kind.PCT)
        if sm is not None:
            a.add("sm_per_boe", "Standardized measure per boe", sm / (boe * 1e6), Kind.PRICE)
    sms = [
        e.standardized_measure_disc_future_cash_flows
        for e in ep
        if e.standardized_measure_disc_future_cash_flows is not None
    ]
    if len(sms) >= 2 and sms[0] > 0:
        a.add("sm_change", "Standardized measure change over window", sms[-1] / sms[0] - 1, Kind.PCT)


def segment_anchors(financials: NormalizedFinancials, segment: str, *, window_years: int = 5) -> Anchors:
    rows = sorted(
        (s for s in financials.segments if s.segment_name == segment and not s.period.is_ttm),
        key=lambda s: s.period.fiscal_year,
    )[-window_years:]
    a = Anchors()
    if not rows:
        return a
    last = rows[-1]
    a.add("segment_revenue", f"Segment revenue FY{last.period.fiscal_year}", last.revenue, Kind.USD)
    total_rev = sum(s.revenue for s in financials.segments if s.period.fiscal_year == last.period.fiscal_year)
    a.add("segment_revenue_share", "Share of total segment revenue", _div(last.revenue, total_rev), Kind.PCT)
    if len(rows) >= 2:
        a.add(
            "revenue_cagr",
            "Segment revenue CAGR",
            _cagr(rows[0].revenue, last.revenue, len(rows) - 1),
            Kind.PCT,
        )
    margins = [
        _div(s.operating_income, s.revenue) for s in rows if s.operating_income is not None and s.revenue > 0
    ]
    a.add("operating_margin_median", "Median segment operating margin", _median(margins), Kind.PCT)
    if last.operating_income is not None:
        a.add(
            "segment_operating_income", "Segment operating income (latest)", last.operating_income, Kind.USD
        )
        if last.depreciation_amortization is not None and last.revenue > 0:
            a.add(
                "segment_ebitda_margin",
                "Segment EBITDA margin (latest)",
                (last.operating_income + last.depreciation_amortization) / last.revenue,
                Kind.PCT,
            )
    if last.capex is not None and last.revenue > 0:
        a.add(
            "capex_to_revenue_median",
            "Segment capex / revenue (latest)",
            abs(last.capex) / last.revenue,
            Kind.PCT,
        )
    if last.assets is not None and last.assets > 0:
        a.add("revenue_to_assets", "Segment revenue / segment assets", last.revenue / last.assets, Kind.RATIO)
    return a


def segment_has_operating_income(financials: NormalizedFinancials, segment: str) -> bool:
    return any(s.segment_name == segment and s.operating_income is not None for s in financials.segments)
