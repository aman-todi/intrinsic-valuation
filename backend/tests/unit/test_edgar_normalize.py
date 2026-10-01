"""Ticket 3: EDGAR normalization against the fixture JSON (no network)."""

from __future__ import annotations

import pytest

from app.data.edgar.normalize import (
    TAG_MAP,
    filer_forms,
    latest_10k,
    latest_periodic_accession,
    normalize,
    window_flags,
)
from app.schemas.financials import DataFlag, NormalizedFinancials
from tests.fixtures.edgar import (
    COMPANYFACTS_TICKERS,
    load_companyfacts,
    load_normalized,
    load_submissions,
)

M = 1_000_000


def approx(x: float, rel: float = 1e-9) -> object:
    return pytest.approx(x, rel=rel)


def fy(nf: NormalizedFinancials, year: int):
    """(income, balance, cashflow) for an annual fiscal year."""
    for i, (inc, bs, cf) in enumerate(
        zip(nf.income_statements, nf.balance_sheets, nf.cash_flows, strict=True)
    ):
        if inc.period.fiscal_year == year and not inc.period.is_ttm:
            return nf.income_statements[i], bs, cf
    raise KeyError(year)


def ttm(nf: NormalizedFinancials):
    return nf.income_statements[-1], nf.balance_sheets[-1], nf.cash_flows[-1]


def has_flag(nf: NormalizedFinancials, fragment: str) -> bool:
    return any(fragment in f for f in nf.data_confidence_flags)


@pytest.fixture(scope="module")
def nfs() -> dict[str, NormalizedFinancials]:
    return {t: load_normalized(t) for t in COMPANYFACTS_TICKERS}


# ---------------------------------------------------------------------------------------------------
# Structural invariants for every fixture
# ---------------------------------------------------------------------------------------------------


def test_structure(nfs: dict[str, NormalizedFinancials]) -> None:
    """Every companyfacts fixture normalizes to aligned, ordered annual rows plus one TTM row."""
    for ticker in COMPANYFACTS_TICKERS:
        _check_structure(ticker, nfs[ticker])


def _check_structure(ticker: str, nf: NormalizedFinancials) -> None:
    assert nf.ticker == ticker
    assert len(nf.cik) == 10 and nf.cik.isdigit()
    assert len(nf.income_statements) == len(nf.balance_sheets) == len(nf.cash_flows) >= 3
    periods = [i.period for i in nf.income_statements]
    assert periods[-1].is_ttm and not any(p.is_ttm for p in periods[:-1])
    ends = [p.period_end for p in periods[:-1]]
    assert ends == sorted(ends) and len(set(ends)) == len(ends)
    assert periods[-1].period_end >= ends[-1]
    for inc, bs, cf in zip(nf.income_statements, nf.balance_sheets, nf.cash_flows, strict=True):
        assert inc.period == bs.period == cf.period
    assert nf.accession_number == latest_periodic_accession(load_submissions(ticker))
    # round-trips through the contract
    assert NormalizedFinancials.model_validate_json(nf.model_dump_json()) == nf


# ---------------------------------------------------------------------------------------------------
# AAPL: September FYE, tag switch, restated split-adjusted shares, TTM from a Q3 10-Q
# ---------------------------------------------------------------------------------------------------


def test_aapl_annual_figures(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["AAPL"]
    assert nf.fiscal_year_end_month == 9
    inc, bs, cf = fy(nf, 2024)
    assert inc.period.period_end == "2024-09-28"
    assert inc.revenue == 391_035 * M
    assert inc.net_income == 93_736 * M
    assert inc.operating_income == 123_216 * M
    assert inc.gross_profit == 180_683 * M
    assert inc.diluted_shares == 15_408_095_000
    assert bs.total_debt == (85_750 + 10_912 + 9_967) * M  # noncurrent + current LTD + commercial paper
    assert bs.operating_lease_liability == (1_488 + 10_046) * M
    assert bs.short_term_investments == 35_228 * M
    assert cf.change_in_nwc == -3_651 * M  # components: AR+other rec+inventory+other assets - AP - other liab
    assert cf.capex == 9_447 * M
    # Apple stopped tagging interest expense in FY2024 -> assumed 0 and flagged
    assert inc.interest_expense == 0
    assert has_flag(nf, "interest_expense: not reported, assumed 0 (FY2024, TTM)")


def test_aapl_ttm_non_december_fye(nfs: dict[str, NormalizedFinancials]) -> None:
    inc, bs, cf = ttm(nfs["AAPL"])
    assert inc.period.is_ttm and inc.period.period_end == "2025-06-28" and inc.period.fiscal_year == 2025
    # FY2024 + 9M FY2025 - 9M FY2024
    assert inc.revenue == (391_035 + (124_300 + 95_359 + 94_036) - (119_575 + 90_753 + 85_777)) * M
    assert inc.revenue == 408_625 * M
    assert inc.net_income == 99_280 * M
    assert inc.operating_income == 130_214 * M
    assert inc.diluted_shares == 14_948_179_000  # latest 3-month diluted shares
    assert bs.cash_and_equivalents == 36_269 * M  # balance sheet at the 10-Q date
    assert bs.total_debt == (82_430 + 10_916 + 9_923) * M


# ---------------------------------------------------------------------------------------------------
# MSFT: June FYE, no 10-Q after the latest 10-K, derived SG&A, finance leases in debt
# ---------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------
# SNOW: January FYE, loss-making, Q1-only YTD
# ---------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------
# PFE: restatement for discontinued operations (latest filed wins), DebtCurrent, pension deficit
# ---------------------------------------------------------------------------------------------------


def test_restatement_latest_filed_wins_synthetic() -> None:
    cf = load_companyfacts("HON")
    facts = cf["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    fy24 = [f for f in facts if f["end"] == "2024-12-31" and f.get("start") == "2024-01-01"]
    assert fy24
    late = dict(fy24[0], val=40_000 * M, filed="2026-02-10", accn="0000773840-26-000001", form="10-K/A")
    early = dict(fy24[0], val=1 * M, filed="2020-01-01", accn="0000773840-20-000001")
    facts.extend([late, early])
    nf = normalize(cf, load_submissions("HON"), "HON")
    assert fy(nf, 2024)[0].revenue == 40_000 * M


# ---------------------------------------------------------------------------------------------------
# HON: Revenues tag (fallback), derived gross profit and operating income, pension surplus
# ---------------------------------------------------------------------------------------------------


def test_tag_fallback_order_prefers_first_tag() -> None:
    cf = load_companyfacts("HON")
    # add an RevenueFromContract... series with a different FY2024 value: it outranks `Revenues`
    rev = cf["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    fy24 = next(f for f in rev if f["end"] == "2024-12-31" and f.get("start") == "2024-01-01")
    cf["facts"]["us-gaap"][TAG_MAP["revenue"][0]] = {"units": {"USD": [dict(fy24, val=38_000 * M)]}}
    nf = normalize(cf, load_submissions("HON"), "HON")
    assert fy(nf, 2024)[0].revenue == 38_000 * M
    assert fy(nf, 2023)[0].revenue == 36_662 * M  # still falls back to Revenues where the first tag is absent


# ---------------------------------------------------------------------------------------------------
# Financials: JPM (bank), TRV (P&C), MET (life)
# ---------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------
# O (REIT), EOG (E&P), DUK (utility), EPD (MLP), NEM, VKTX, ALAB, CVII
# ---------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------
# Helpers for the classifier
# ---------------------------------------------------------------------------------------------------


def test_foreign_filer_submissions_helpers() -> None:
    sub = load_submissions("TSM")
    forms = filer_forms(sub)
    assert {"20-F", "6-K"} <= forms and not forms & {"10-K", "10-Q"}
    assert latest_periodic_accession(sub) is None
    assert latest_10k(sub) is None


def test_window_flags_keep_only_the_models_window():
    """Flags outside the last N fiscal years (+ TTM) are dropped, period lists are cut to the window,
    methodology notes are hidden unless asked for, and the full list stays on the financials."""
    base = load_normalized("AAPL")  # FY2015-FY2024 + TTM
    nf = base.model_copy(
        update={
            "data_flags": [
                DataFlag(
                    message="interest_expense: not reported, assumed 0",
                    periods=["FY2016", "FY2023", "FY2024", "TTM"],
                ),
                DataFlag(
                    message="operating_lease_liability: not reported, assumed 0",
                    periods=["FY2015", "FY2016", "FY2017"],
                ),
                DataFlag(message="revenue jump FY2016 (+60% vs FY2015)", scope=["FY2016"]),
                DataFlag(message="revenue jump FY2022 (+55% vs FY2021)", scope=["FY2022"]),
                DataFlag(
                    message="total_debt: derived as sum of A + B",
                    periods=["FY2020", "FY2021", "FY2022", "FY2023", "FY2024", "TTM"],
                    note=True,
                ),
                DataFlag(message="only 4 fiscal years of annual data available"),
            ]
        }
    )
    assert window_flags(nf, 5) == [
        "interest_expense: not reported, assumed 0 (FY2023, FY2024, TTM)",
        "revenue jump FY2022 (+55% vs FY2021)",
        "only 4 fiscal years of annual data available",
    ]
    assert "total_debt: derived as sum of A + B (FY2020-FY2024, TTM)" in window_flags(
        nf, 5, include_notes=True
    )
    assert len(window_flags(nf, 10)) == 5  # the 10-year window reaches FY2015-FY2016 again
    # Structured flags render to exactly the legacy full-history list.
    assert len(base.data_flags) == len(base.data_confidence_flags)
    # Financials without structured flags fall back to the full list.
    legacy = base.model_copy(update={"data_flags": []})
    assert window_flags(legacy, 5) == base.data_confidence_flags
