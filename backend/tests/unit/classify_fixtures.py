"""Synthetic ClassificationSignals fixtures for the classifier tests.

Numbers are rough, company-*like* approximations (USD) — not recorded EDGAR data. The same
expectations are asserted against the EDGAR fixtures through the real classify-job data path in
``test_fixture_reconciliation.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from app.classify.rules import (
    TAG_DEPOSITS,
    TAG_NII,
    TAG_PREMIUMS,
    TAG_REAL_ESTATE,
    TAG_SMOG,
    ClassificationSignals,
)
from app.schemas.company import CompanySnapshot, DeclineReason, ModelType
from app.schemas.financials import (
    BalanceSheetLine,
    BankSpecificLine,
    CashFlowLine,
    FiscalPeriod,
    IncomeStatementLine,
    NormalizedFinancials,
    SegmentLine,
)

B = 1e9
M = 1e6
LAST_FY = 2025
DOMESTIC = frozenset({"10-K", "10-Q", "8-K"})


def _series(v: float | Sequence[float], n: int) -> list[float]:
    if isinstance(v, int | float):
        return [float(v)] * n
    vals = list(v)
    assert len(vals) == n, f"expected {n} values, got {len(vals)}"
    return [float(x) for x in vals]


def grow(start: float, rates: float | Sequence[float], n: int) -> list[float]:
    """n revenues starting at ``start`` compounding by ``rates`` (scalar or n-1 values)."""
    rs = _series(rates, n - 1) if n > 1 else []
    out = [start]
    for r in rs:
        out.append(out[-1] * (1 + r))
    return out


def seg(name: str, revenue: float, op_income: float | None, fy: int = LAST_FY, capex: float | None = None):
    return SegmentLine(
        period=FiscalPeriod(fiscal_year=fy, period_end=f"{fy}-12-31"),
        segment_name=name,
        revenue=revenue,
        operating_income=op_income,
        depreciation_amortization=None,
        capex=capex,
        assets=None,
    )


def make_financials(
    ticker: str,
    *,
    revenues: Sequence[float],
    op_margins: float | Sequence[float] = 0.15,
    rd_ratio: float = 0.0,
    rd_abs: float | Sequence[float] | None = None,
    total_debt: float | Sequence[float] = 0.0,
    total_equity: float | Sequence[float] = 1 * B,
    include_ttm: bool = True,
    segments: Sequence[SegmentLine] = (),
    bank_data: Sequence[BankSpecificLine] = (),
    flags: Sequence[str] = (),
    last_fy: int = LAST_FY,
) -> NormalizedFinancials:
    """Build NormalizedFinancials from a handful of parameters. TTM row (if any) repeats the last FY."""
    n = len(revenues)
    margins = _series(op_margins, n)
    debts = _series(total_debt, n)
    equities = _series(total_equity, n)
    rds = _series(rd_abs, n) if rd_abs is not None else [r * rd_ratio for r in revenues]
    years = list(range(last_fy - n + 1, last_fy + 1))

    inc, bal, cfs = [], [], []
    for i, fy in enumerate(years):
        p = FiscalPeriod(fiscal_year=fy, period_end=f"{fy}-12-31")
        rev = float(revenues[i])
        oi = rev * margins[i] if rev > 0 else -rds[i]
        interest = debts[i] * 0.05
        pretax = oi - interest
        tax = max(pretax, 0) * 0.21
        inc.append(
            IncomeStatementLine(
                period=p,
                revenue=rev,
                cogs=None,
                gross_profit=None,
                sga=None,
                rd=rds[i],
                operating_income=oi,
                interest_expense=interest,
                pretax_income=pretax,
                tax_expense=tax,
                net_income=pretax - tax,
                diluted_shares=1e9,
            )
        )
        bal.append(
            BalanceSheetLine(
                period=p,
                cash_and_equivalents=0.1 * max(rev, 1.0),
                short_term_investments=0.0,
                total_debt=debts[i],
                operating_lease_liability=0.0,
                total_equity=equities[i],
                minority_interest=0.0,
                preferred_equity=0.0,
                pension_deficit=None,
            )
        )
        cfs.append(
            CashFlowLine(
                period=p,
                depreciation_amortization=0.04 * rev,
                stock_based_comp=0.01 * rev,
                capex=0.05 * rev,
                change_in_nwc=0.0,
            )
        )
    if include_ttm and inc:
        last = inc[-1]
        ttm_p = FiscalPeriod(fiscal_year=last_fy, period_end=f"{last_fy + 1}-06-30", is_ttm=True)
        inc.append(last.model_copy(update={"period": ttm_p}))
    return NormalizedFinancials(
        ticker=ticker,
        cik="0000000000",
        fiscal_year_end_month=12,
        income_statements=inc,
        balance_sheets=bal,
        cash_flows=cfs,
        segments=list(segments),
        bank_data=list(bank_data),
        data_confidence_flags=list(flags),
        accession_number=f"0000000000-25-{ticker}",
    )


def make_signals(
    ticker: str,
    name: str,
    sic: str,
    sic_desc: str,
    market_cap: float,
    financials: NormalizedFinancials,
    *,
    forms: frozenset[str] = DOMESTIC,
    entity_type: str = "operating",
    tags: Mapping[str, float] | None = None,
    extra_tags: Sequence[str] = (),
    years_public: float | None = 20.0,
    ocf: Sequence[float] = (),
) -> ClassificationSignals:
    tag_values = dict(tags or {})
    return ClassificationSignals(
        company=CompanySnapshot(
            ticker=ticker,
            cik=financials.cik,
            name=name,
            sic_code=sic,
            sic_description=sic_desc,
            market_cap_usd=market_cap,
        ),
        financials=financials,
        filer_forms=forms,
        entity_type=entity_type,
        present_tags=frozenset({*tag_values, *extra_tags, "Revenues"}),
        tag_latest_values=tag_values,
        years_public=years_public,
        operating_cash_flow_history=tuple(ocf),
    )


@dataclass(frozen=True)
class Expected:
    model: ModelType | None = None
    decline: DeclineReason | None = None
    runner_up: ModelType | None = None
    early_stage: bool = False
    window_years: int | None = None
    sotp_segments: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------------------


def aapl() -> ClassificationSignals:
    fin = make_financials(
        "AAPL",
        revenues=[365 * B, 394 * B, 383 * B, 391 * B, 416 * B],
        op_margins=[0.30, 0.30, 0.30, 0.31, 0.32],
        rd_ratio=0.08,
        total_debt=[125 * B, 120 * B, 111 * B, 107 * B, 98 * B],
        total_equity=[63 * B, 50 * B, 62 * B, 57 * B, 70 * B],
    )
    return make_signals("AAPL", "Apple Inc.", "3571", "Electronic Computers", 3.4e12, fin)


def msft() -> ClassificationSignals:
    fin = make_financials(
        "MSFT",
        revenues=grow(110 * B, 0.13, 10),
        op_margins=[0.40, 0.41, 0.42, 0.42, 0.43, 0.42, 0.44, 0.45, 0.45, 0.45],
        rd_ratio=0.12,
        total_debt=60 * B,
        total_equity=[80 * B + 25 * B * i for i in range(10)],
        segments=[
            seg("Productivity and Business Processes", 110 * B, 49.5 * B),
            seg("Intelligent Cloud", 120 * B, 52.8 * B),
            seg("More Personal Computing", 70 * B, 29.4 * B),
        ],
    )
    return make_signals("MSFT", "Microsoft Corporation", "7372", "Prepackaged Software", 3.6e12, fin)


def jpm() -> ClassificationSignals:
    revs = grow(100 * B, 0.06, 10)
    fin = make_financials(
        "JPM",
        revenues=revs,
        op_margins=0.35,
        total_debt=500 * B,
        total_equity=grow(250 * B, 0.03, 10),
        bank_data=[
            BankSpecificLine(
                period=FiscalPeriod(fiscal_year=LAST_FY, period_end=f"{LAST_FY}-12-31"),
                net_interest_income=92 * B,
                provision_for_credit_losses=10 * B,
                total_deposits=2400 * B,
                tangible_book_value=280 * B,
                tier1_capital_ratio=0.15,
            )
        ],
    )
    return make_signals(
        "JPM",
        "JPMorgan Chase & Co.",
        "6021",
        "National Commercial Banks",
        700 * B,
        fin,
        tags={TAG_DEPOSITS: 2400 * B, TAG_NII: 92 * B},
    )


def trv() -> ClassificationSignals:
    fin = make_financials(
        "TRV",
        revenues=grow(30 * B, 0.05, 10),
        op_margins=[0.10, 0.12, 0.09, 0.11, 0.10, 0.08, 0.12, 0.07, 0.10, 0.13],
        total_debt=8 * B,
        total_equity=grow(22 * B, 0.03, 10),
    )
    return make_signals(
        "TRV",
        "The Travelers Companies, Inc.",
        "6331",
        "Fire, Marine & Casualty Insurance",
        55 * B,
        fin,
        tags={TAG_PREMIUMS: 41 * B},
    )


def realty_income() -> ClassificationSignals:
    fin = make_financials(
        "O",
        revenues=grow(2.6 * B, 0.18, 5),
        op_margins=0.42,
        total_debt=[20 * B, 22 * B, 24 * B, 26 * B, 27 * B],
        total_equity=[30 * B, 33 * B, 35 * B, 37 * B, 39 * B],
    )
    return make_signals(
        "O",
        "Realty Income Corporation",
        "6798",
        "Real Estate Investment Trusts",
        50 * B,
        fin,
        tags={TAG_REAL_ESTATE: 50 * B},
    )


def eog() -> ClassificationSignals:
    fin = make_financials(
        "EOG",
        revenues=[11 * B, 18.6 * B, 25.7 * B, 24.2 * B, 23.4 * B, 22.6 * B],
        op_margins=[0.05, 0.33, 0.43, 0.37, 0.35, 0.33],
        total_debt=5 * B,
        total_equity=[20 * B, 22 * B, 24 * B, 28 * B, 29 * B, 30 * B],
    )
    return make_signals(
        "EOG",
        "EOG Resources, Inc.",
        "1311",
        "Crude Petroleum & Natural Gas",
        70 * B,
        fin,
        tags={TAG_SMOG: 60 * B},
    )


def hon() -> ClassificationSignals:
    fin = make_financials(
        "HON",
        revenues=grow(34 * B, 0.03, 5),
        op_margins=0.20,
        total_debt=25 * B,
        total_equity=18 * B,
        segments=[
            seg("Aerospace Technologies", 15.5 * B, 4.2 * B),
            seg("Industrial Automation", 10.0 * B, 1.8 * B),
            seg("Building Automation", 6.5 * B, 1.6 * B),
            seg("Energy and Sustainability Solutions", 6.3 * B, 0.45 * B),
        ],
    )
    return make_signals("HON", "Honeywell International Inc.", "3714", "Motor Vehicle Parts", 140 * B, fin)


def pfe() -> ClassificationSignals:
    fin = make_financials(
        "PFE",
        revenues=[41.9 * B, 81.3 * B, 100.3 * B, 58.5 * B, 63.6 * B],
        op_margins=[0.22, 0.30, 0.38, 0.05, 0.20],
        rd_ratio=0.17,
        total_debt=[37 * B, 36 * B, 35 * B, 71 * B, 64 * B],
        total_equity=[63 * B, 77 * B, 95 * B, 89 * B, 88 * B],
    )
    return make_signals(
        "PFE",
        "Pfizer Inc.",
        "2834",
        "Pharmaceutical Preparations",
        150 * B,
        fin,
        ocf=(14 * B, 33 * B, 29 * B, 8.7 * B, 12.7 * B),
    )


def snow() -> ClassificationSignals:
    fin = make_financials(
        "SNOWL",
        revenues=[0.59 * B, 1.22 * B, 2.07 * B, 2.81 * B, 3.63 * B],
        op_margins=[-0.92, -0.59, -0.39, -0.37, -0.40],
        rd_ratio=0.45,
        total_debt=0.3 * B,
        total_equity=[5 * B, 5.5 * B, 5.4 * B, 5.2 * B, 3.0 * B],
    )
    return make_signals(
        "SNOWL",
        "Snowlike Data Cloud Inc.",
        "7372",
        "Prepackaged Software",
        60 * B,
        fin,
        years_public=5.0,
        ocf=(-0.05 * B, 0.1 * B, 0.5 * B, 0.85 * B, 0.95 * B),
    )


def biotech() -> ClassificationSignals:
    fin = make_financials(
        "PREB",
        revenues=[0.0, 0.0, 2 * M, 5 * M, 4 * M, 6 * M],
        op_margins=-50.0,
        rd_abs=[180 * M, 220 * M, 260 * M, 300 * M, 320 * M, 340 * M],
        total_debt=0.0,
        total_equity=[900 * M, 800 * M, 700 * M, 600 * M, 500 * M, 450 * M],
    )
    return make_signals(
        "PREB",
        "Prebiotica Therapeutics, Inc.",
        "2836",
        "Biological Products",
        2 * B,
        fin,
        years_public=6.0,
        ocf=(-170 * M, -210 * M, -250 * M, -280 * M, -300 * M, -320 * M),
    )


def ipo_two_years() -> ClassificationSignals:
    fin = make_financials(
        "NEWCO",
        revenues=[800 * M, 1.1 * B],
        op_margins=[0.05, 0.08],
        total_debt=200 * M,
        total_equity=1 * B,
    )
    return make_signals(
        "NEWCO", "Newco Holdings, Inc.", "7370", "Computer Services", 8 * B, fin, years_public=0.8
    )


def foreign_20f() -> ClassificationSignals:
    fin = make_financials("TSMX", revenues=grow(45 * B, 0.15, 5), op_margins=0.42, total_equity=100 * B)
    return make_signals(
        "TSMX",
        "Taiwan Semiconductor-like Co., Ltd.",
        "3674",
        "Semiconductors & Related Devices",
        900 * B,
        fin,
        forms=frozenset({"20-F", "6-K"}),
    )


def met() -> ClassificationSignals:
    fin = make_financials("MET", revenues=grow(68 * B, 0.02, 10), op_margins=0.08, total_equity=30 * B)
    return make_signals(
        "MET",
        "MetLife, Inc.",
        "6311",
        "Life Insurance",
        55 * B,
        fin,
        tags={TAG_PREMIUMS: 45 * B},
    )


def epd() -> ClassificationSignals:
    fin = make_financials(
        "EPD", revenues=grow(40 * B, 0.05, 10), op_margins=0.12, total_debt=30 * B, total_equity=28 * B
    )
    return make_signals(
        "EPD",
        "Enterprise Products Partners L.P.",
        "4922",
        "Natural Gas Transmission",
        70 * B,
        fin,
    )


def nem() -> ClassificationSignals:
    fin = make_financials("NEM", revenues=grow(11 * B, 0.08, 10), op_margins=0.20, total_equity=30 * B)
    return make_signals("NEM", "Newmont Corporation", "1040", "Gold and Silver Ores", 60 * B, fin)


def spac() -> ClassificationSignals:
    fin = make_financials(
        "SPCX",
        revenues=[0.0, 0.0, 0.0],
        rd_abs=0.0,
        total_equity=5 * M,
    )
    return make_signals(
        "SPCX",
        "Example Growth Acquisition Corp.",
        "6770",
        "Blank Checks",
        250 * M,
        fin,
        years_public=3.0,
    )


def royalty_trust() -> ClassificationSignals:
    fin = make_financials("PBTX", revenues=grow(40 * M, 0.02, 5), op_margins=0.95, total_equity=2 * M)
    return make_signals(
        "PBTX",
        "Permian-like Basin Royalty Trust",
        "6792",
        "Oil Royalty Traders",
        500 * M,
        fin,
        entity_type="other",
    )


def utility() -> ClassificationSignals:
    fin = make_financials(
        "UTIL",
        revenues=grow(28 * B, 0.03, 5),
        op_margins=[0.24, 0.25, 0.24, 0.25, 0.26],
        total_debt=[70 * B, 73 * B, 76 * B, 79 * B, 82 * B],
        total_equity=[48 * B, 50 * B, 52 * B, 54 * B, 56 * B],
    )
    return make_signals(
        "UTIL", "Stable Regulated Utility Corp", "4931", "Electric & Other Services Combined", 70 * B, fin
    )


def extreme_levered() -> ClassificationSignals:
    fin = make_financials(
        "LEVX",
        revenues=grow(10 * B, 0.02, 5),
        op_margins=0.30,
        total_debt=[30 * B, 30.5 * B, 31 * B, 31.5 * B, 32 * B],
        total_equity=[12 * B, 12.2 * B, 12.4 * B, 12.6 * B, 12.8 * B],
    )
    return make_signals("LEVX", "Levered Stable Holdings Inc.", "4911", "Electric Services", 10 * B, fin)


def steel() -> ClassificationSignals:
    fin = make_financials(
        "NUE",
        revenues=[25 * B, 20 * B, 22 * B, 25 * B, 23 * B, 20 * B, 36 * B, 41 * B, 34 * B, 30 * B],
        op_margins=[0.08, 0.06, 0.09, 0.11, 0.10, 0.06, 0.24, 0.26, 0.19, 0.12],
        total_debt=6 * B,
        total_equity=20 * B,
    )
    return make_signals("NUE", "Nucor Corporation", "3312", "Steel Works, Blast Furnaces", 40 * B, fin)


def borderline_sotp() -> ClassificationSignals:
    fin = make_financials(
        "CONG",
        revenues=grow(18 * B, 0.04, 5),
        op_margins=0.18,
        total_debt=6 * B,
        total_equity=12 * B,
        segments=[
            seg("Industrial", 12 * B, 2.64 * B),  # 22%
            seg("Consumer", 9 * B, 1.035 * B),  # 11.5% -> 10.5pt spread: borderline
        ],
    )
    return make_signals("CONG", "Conglomerate-ish Corp", "3990", "Misc. Manufacturing", 35 * B, fin)


def integrated_oil() -> ClassificationSignals:
    fin = make_financials(
        "XOMX",
        revenues=[265 * B, 180 * B, 285 * B, 400 * B, 345 * B, 340 * B],
        op_margins=[0.07, -0.10, 0.10, 0.17, 0.13, 0.12],
        total_debt=40 * B,
        total_equity=[190 * B, 160 * B, 170 * B, 200 * B, 205 * B, 265 * B],
    )
    return make_signals(
        "XOMX",
        "Integrated Oil-like Corporation",
        "2911",
        "Petroleum Refining",
        450 * B,
        fin,
        tags={TAG_SMOG: 250 * B},
    )


FIXTURES: dict[str, tuple[Callable[[], ClassificationSignals], Expected]] = {
    "AAPL": (aapl, Expected(model=ModelType.FCFF, window_years=5)),
    "MSFT": (msft, Expected(model=ModelType.FCFF, window_years=5)),
    "JPM": (jpm, Expected(model=ModelType.EXCESS_RETURN, window_years=10)),
    "TRV": (trv, Expected(model=ModelType.EXCESS_RETURN, window_years=10)),
    "O": (realty_income, Expected(model=ModelType.NAV_REIT, runner_up=ModelType.FCFF)),
    "EOG": (eog, Expected(model=ModelType.NAV_EP, runner_up=ModelType.FCFF)),
    "HON": (
        hon,
        Expected(
            model=ModelType.SOTP,
            runner_up=ModelType.FCFF,
            sotp_segments=(
                "Aerospace Technologies",
                "Industrial Automation",
                "Building Automation",
                "Energy and Sustainability Solutions",
            ),
        ),
    ),
    "PFE": (pfe, Expected(model=ModelType.FCFF)),
    "SNOWL": (snow, Expected(model=ModelType.FCFF, early_stage=True)),
    "PREB": (biotech, Expected(decline=DeclineReason.BIOTECH_PRECOMMERCIAL)),
    "NEWCO": (ipo_two_years, Expected(decline=DeclineReason.INSUFFICIENT_DATA)),
    "TSMX": (foreign_20f, Expected(decline=DeclineReason.NON_10K_FILER)),
    "MET": (met, Expected(decline=DeclineReason.LIFE_INSURER)),
    "EPD": (epd, Expected(decline=DeclineReason.MLP)),
    "NEM": (nem, Expected(decline=DeclineReason.MINING)),
    "SPCX": (spac, Expected(decline=DeclineReason.SPAC_OR_TRUST)),
    "PBTX": (royalty_trust, Expected(decline=DeclineReason.SPAC_OR_TRUST)),
    "UTIL": (utility, Expected(model=ModelType.FCFF, runner_up=ModelType.FCFE, window_years=5)),
    "LEVX": (extreme_levered, Expected(model=ModelType.FCFE, runner_up=ModelType.FCFF)),
    "NUE": (steel, Expected(model=ModelType.FCFF, window_years=10)),
    "CONG": (borderline_sotp, Expected(model=ModelType.SOTP, runner_up=ModelType.FCFF)),
    "XOMX": (integrated_oil, Expected(model=ModelType.FCFF, runner_up=ModelType.NAV_EP)),
}
