"""Ticket 3: EDGAR normalization against the fixture JSON (no network)."""

from __future__ import annotations

import copy

import pytest

from app.data.edgar.normalize import (
    TAG_MAP,
    NormalizationError,
    entity_type,
    filer_forms,
    fiscal_year_end_month,
    latest_10k,
    latest_annual_values,
    latest_periodic_accession,
    latest_value,
    normalize,
    operating_cash_flow_history,
    present_tags,
    sic,
)
from app.schemas.financials import NormalizedFinancials
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


@pytest.mark.parametrize("ticker", COMPANYFACTS_TICKERS)
def test_structure(ticker: str, nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs[ticker]
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


def test_aapl_tag_switch_fallback_order(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["AAPL"]
    # FY2015/2016 only exist under SalesRevenueNet; FY2017+ under the ASC 606 tag.
    assert fy(nf, 2016)[0].revenue == 215_639 * M
    assert fy(nf, 2015)[0].revenue == 233_715 * M
    assert fy(nf, 2017)[0].revenue == 229_234 * M
    assert fy(nf, 2016)[1].short_term_investments == 46_671 * M  # AvailableForSaleSecuritiesCurrent


def test_aapl_restated_shares_latest_filed_wins(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["AAPL"]
    # FY2018 10-K reported 5,000,109k pre-split; the FY2020 10-K restated it split-adjusted.
    assert fy(nf, 2018)[0].diluted_shares == 20_000_436_000
    assert fy(nf, 2017)[0].diluted_shares == 5_251_692_000  # never restated
    assert has_flag(nf, "possible 4-for-1 split between FY2017 and FY2018")


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


def test_msft(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["MSFT"]
    assert nf.fiscal_year_end_month == 6
    inc, bs, _ = fy(nf, 2025)
    assert inc.revenue == 281_724 * M
    assert inc.sga == (25_654 + 7_223) * M
    assert has_flag(nf, "sga: derived as selling & marketing + general & administrative")
    assert bs.total_debt == (40_152 + 2_999 + 3_236 + 47_856) * M
    t_inc, t_bs, _ = ttm(nf)
    # 10-Qs for FY2025 pre-date the FY2025 10-K -> TTM equals FY2025
    assert t_inc.revenue == 281_724 * M and t_inc.period.period_end == "2025-06-30"
    assert t_bs.total_debt == bs.total_debt
    assert has_flag(nf, "TTM: no 10-Q after FY2025")


# ---------------------------------------------------------------------------------------------------
# SNOW: January FYE, loss-making, Q1-only YTD
# ---------------------------------------------------------------------------------------------------


def test_snow_january_fye_ttm(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["SNOW"]
    assert nf.fiscal_year_end_month == 1
    inc, bs, _ = fy(nf, 2025)
    assert inc.period.period_end == "2025-01-31"
    assert inc.revenue == approx(3_626_396_000)
    assert inc.operating_income == approx(-1_456_300_000)
    assert bs.total_debt == approx(2_270_400_000)  # convertible notes
    assert fy(nf, 2024)[1].total_debt == 0
    t_inc, _, _ = ttm(nf)
    assert t_inc.period.period_end == "2025-04-30" and t_inc.period.fiscal_year == 2026
    assert t_inc.revenue == approx(3_626_396_000 + 1_042_137_000 - 828_709_000)
    assert t_inc.operating_income == approx((-1_456.3 - 443.8 + 325.7) * M)
    assert t_inc.net_income == approx((-1_285.6 - 430.1 + 317.0) * M)
    assert [i.period.fiscal_year for i in nf.income_statements[:-1]] == list(range(2020, 2026))


# ---------------------------------------------------------------------------------------------------
# PFE: restatement for discontinued operations (latest filed wins), DebtCurrent, pension deficit
# ---------------------------------------------------------------------------------------------------


def test_pfe_restatement(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["PFE"]
    assert fy(nf, 2018)[0].revenue == 40_825 * M  # originally 53,647 in the 2018 10-K
    assert fy(nf, 2019)[0].revenue == 41_172 * M  # originally 51,750
    assert fy(nf, 2019)[0].cogs == 8_061 * M
    assert fy(nf, 2017)[0].revenue == 52_546 * M  # never recast (outside the 2020 10-K window)
    inc, bs, _ = fy(nf, 2024)
    assert inc.revenue == 63_627 * M
    assert inc.operating_income == (8_023 + 3_091) * M  # derived: pretax + interest
    assert bs.total_debt == (57_020 + 6_946) * M
    assert bs.pension_deficit == 1_100 * M
    assert operating_cash_flow_history(load_companyfacts("PFE"))[-1] == 12_744 * M


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


def test_hon(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["HON"]
    inc, bs, cf = fy(nf, 2024)
    assert inc.revenue == 38_498 * M
    assert inc.gross_profit == (38_498 - 23_870) * M
    assert inc.operating_income == (7_222 + 1_058) * M
    assert has_flag(nf, "gross_profit: derived as revenue - cogs")
    assert has_flag(nf, "operating_income: derived as pretax income + interest expense")
    assert bs.pension_deficit == 0  # overfunded
    assert bs.minority_interest == 434 * M
    assert bs.total_debt == (25_479 + 2_698 + 4_275) * M
    assert cf.change_in_nwc == 1_140 * M  # IncreaseDecreaseInOperatingCapital used directly
    t_inc, _, _ = ttm(nf)
    assert t_inc.revenue == (38_498 + 9_822 + 10_352 - 9_105 - 9_578) * M
    assert t_inc.net_income == (5_705 + 1_450 + 1_570 - 1_468 - 1_544) * M


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


def test_jpm_bank(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["JPM"]
    inc, bs, _ = fy(nf, 2024)
    assert inc.revenue == 177_556 * M
    assert inc.operating_income == inc.pretax_income == 75_081 * M
    assert bs.total_debt == (401_418 + 52_893) * M
    assert bs.cash_and_equivalents == 23_372 * M  # CashAndDueFromBanks
    bank = {b.period.period_end: b for b in nf.bank_data}
    b24 = bank["2024-12-31"]
    assert b24.net_interest_income == 92_583 * M
    assert b24.total_deposits == 2_406_032 * M
    assert b24.tangible_book_value == (344_758 - 20_050 - 52_565 - 1_407) * M
    assert b24.tier1_capital_ratio == pytest.approx(0.168)
    b_ttm = nf.bank_data[-1]
    assert b_ttm.period.is_ttm
    assert b_ttm.net_interest_income == (92_583 + 23_273 + 23_209 - 23_082 - 22_746) * M
    assert b_ttm.total_deposits == 2_562_580 * M
    assert ttm(nf)[0].net_income == 56_533 * M
    assert len(nf.bank_data) == 9 and not nf.insurer_data


def test_trv_pc_insurer(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["TRV"]
    inc, _, _ = fy(nf, 2024)
    # small RevenueFromContract... (fee income) is ignored in favour of total Revenues
    assert inc.revenue == 46_423 * M
    assert has_flag(
        nf, "revenue: using Revenues instead of RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    ins = {i.period.period_end: i for i in nf.insurer_data}["2024-12-31"]
    assert ins.net_premiums_earned == 41_501 * M
    assert ins.loss_and_lae_ratio == approx(27_265 / 41_501)
    assert ins.combined_ratio == approx((27_265 + 6_655 + 5_433) / 41_501)
    assert ins.book_value_per_share == approx(27_864 / 227.4)
    assert nf.insurer_data[-1].net_premiums_earned == (41_501 + 10_650 + 10_796 - 10_012 - 10_289) * M


def test_met_life_insurer(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["MET"]
    assert sic(load_submissions("MET")) == "6311"
    assert nf.insurer_data and all(i.combined_ratio is None for i in nf.insurer_data)
    assert fy(nf, 2024)[0].net_income == 4_426 * M


# ---------------------------------------------------------------------------------------------------
# O (REIT), EOG (E&P), DUK (utility), EPD (MLP), NEM, VKTX, ALAB, CVII
# ---------------------------------------------------------------------------------------------------


def test_o_reit(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["O"]
    reit = {r.period.period_end: r for r in nf.reit_data}["2024-12-31"]
    assert reit.real_estate_investments_gross == 62_400 * M
    assert reit.accumulated_depreciation == 8_538 * M
    assert reit.ffo == approx((860.8 + 2_395.3 - 62.7) * M)
    assert reit.affo is None
    inc, _, cf = fy(nf, 2024)
    assert inc.pretax_income == approx((860.8 + 66.9) * M)
    assert cf.capex == 3_500 * M
    assert has_flag(nf, "capex: proxy tag PaymentsToAcquireRealEstate")
    assert has_flag(nf, "ffo: derived")


def test_eog_ep(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["EOG"]
    assert len(nf.ep_data) == 8 and not any(e.period.is_ttm for e in nf.ep_data)
    e24 = nf.ep_data[-1]
    assert e24.standardized_measure_disc_future_cash_flows == 33_970 * M
    assert e24.proved_reserves_oil_mmbbl == 1_573
    assert e24.proved_reserves_gas_bcf == 8_650
    # interest expense tag switched from InterestExpense to InterestExpenseNonoperating in the 2021 10-K
    assert fy(nf, 2018)[0].interest_expense == 245 * M
    assert fy(nf, 2024)[0].interest_expense == 139 * M
    assert fy(nf, 2024)[2].capex == 6_026 * M  # PaymentsToAcquireOilAndGasPropertyAndEquipment
    assert (
        "StandardizedMeasureOfDiscountedFutureNetCashFlowsRelatingToProvedOilAndGasReserves"
        in present_tags(load_companyfacts("EOG"))
    )


def test_duk_utility(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["DUK"]
    inc, bs, cf = fy(nf, 2024)
    assert inc.revenue == 30_357 * M
    assert cf.depreciation_amortization == 5_864 * M  # DepreciationAmortizationAndAccretionNet fallback
    assert cf.capex == 12_316 * M  # PaymentsToAcquireProductiveAssets fallback
    assert bs.total_debt == (76_340 + 5_293 + 4_700 + 139 + 1_099) * M
    assert bs.preferred_equity == 973 * M
    assert bs.pension_deficit == 150 * M
    assert fy(nf, 2017)[1].total_debt == (49_035 + 3_244 + 2_788) * M  # pre-ASC 842: no finance leases


def test_epd_mlp(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["EPD"]
    inc, bs, _ = fy(nf, 2024)
    assert inc.diluted_shares == 2_195 * M  # WeightedAverageLimitedPartnershipUnitsOutstandingDiluted
    assert bs.total_equity == 28_583 * M  # PartnersCapital
    sub = load_submissions("EPD")
    assert "L.P." in sub["name"] and sic(sub) == "4922" and entity_type(sub) == "operating"


def test_nem_miner(nfs: dict[str, NormalizedFinancials]) -> None:
    assert sic(load_submissions("NEM")) == "1040"
    inc = fy(nfs["NEM"], 2024)[0]
    assert inc.revenue == 18_682 * M and inc.net_income == 3_348 * M


def test_vktx_pre_revenue_biotech(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["VKTX"]
    assert all(i.revenue == 0 for i in nf.income_statements)
    assert has_flag(nf, "revenue: not reported, assumed 0")
    assert fy(nf, 2024)[0].rd == approx(101.9 * M)
    assert ttm(nf)[0].rd == approx((101.9 + 36.4 + 65.6 - 23.4 - 22.8) * M)
    ocf = operating_cash_flow_history(load_companyfacts("VKTX"))
    assert len(ocf) == 7 and all(v < 0 for v in ocf)


def test_alab_recent_ipo(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["ALAB"]
    assert len(nf.income_statements) == 3  # FY2023, FY2024, TTM
    assert has_flag(nf, "only 2 fiscal years of annual data available")
    assert ttm(nf)[0].revenue == approx((396.3 + 159.4 + 191.9 - 65.3 - 76.9) * M)


def test_cvii_spac(nfs: dict[str, NormalizedFinancials]) -> None:
    nf = nfs["CVII"]
    assert sic(load_submissions("CVII")) == "6770"
    assert all(i.revenue == 0 for i in nf.income_statements)
    assert latest_value(load_companyfacts("CVII"), "AssetsHeldInTrustNoncurrent") == approx(612.4 * M)


# ---------------------------------------------------------------------------------------------------
# Helpers for the classifier
# ---------------------------------------------------------------------------------------------------


def test_foreign_filer_submissions_helpers() -> None:
    sub = load_submissions("TSM")
    forms = filer_forms(sub)
    assert {"20-F", "6-K"} <= forms and not forms & {"10-K", "10-Q"}
    assert latest_periodic_accession(sub) is None
    assert latest_10k(sub) is None


def test_submissions_helpers() -> None:
    sub = load_submissions("AAPL")
    assert fiscal_year_end_month(sub) == 9
    assert sic(sub) == "3571" and entity_type(sub) == "operating"
    assert {"10-K", "10-Q", "8-K", "4"} <= filer_forms(sub)
    accn, doc, report = latest_10k(sub)  # type: ignore[misc]
    assert report == "2024-09-28" and doc == "aapl-20240928.htm" and accn.startswith("0000320193-24-")
    assert fiscal_year_end_month({"fiscalYearEnd": "0102"}) == 12  # 52/53-week December year
    assert fiscal_year_end_month({"fiscalYearEnd": "0131"}) == 1
    assert fiscal_year_end_month({}) == 12


def test_present_tags_and_latest_values() -> None:
    cf = load_companyfacts("JPM")
    tags = present_tags(cf)
    assert {"Deposits", "InterestIncomeExpenseNet", "EntityCommonStockSharesOutstanding"} <= tags
    vals = latest_annual_values(cf, ["Deposits", "InterestIncomeExpenseNet", "PremiumsEarnedNet"])
    assert vals == {"Deposits": 2_406_032 * M, "InterestIncomeExpenseNet": 92_583 * M}


def test_no_annual_data_raises() -> None:
    cf = copy.deepcopy(load_companyfacts("AAPL"))
    cf["facts"] = {"us-gaap": {}}
    with pytest.raises(NormalizationError):
        normalize(cf, load_submissions("AAPL"), "AAPL")
