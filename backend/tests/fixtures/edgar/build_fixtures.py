"""Builds the EDGAR fixture JSON in this directory.

    cd backend && .venv/bin/python -m tests.fixtures.edgar.build_fixtures

The build sandbox cannot reach data.sec.gov, so these are HAND-BUILT fixtures in the exact SEC JSON
formats (companyfacts / submissions / company_tickers), not recordings. Headline figures (revenue, net
income, operating income, shares, latest-quarter revenue) follow the real filings as closely as known;
secondary lines are approximations. Quarterly values that are not listed explicitly are derived from
the annual line's ratio to revenue. Realistic messiness is reproduced on purpose:

- every 10-K carries 3 years of income-statement comparatives (2 for the EGC recent IPO) and 2 balance
  sheets, so each period appears in several filings (duplicates with later `filed` dates);
- tag switches (AAPL SalesRevenueNet -> RevenueFromContractWithCustomerExcludingAssessedTax in the
  FY2019 10-K; AAPL AvailableForSaleSecuritiesCurrent -> MarketableSecuritiesCurrent; EOG InterestExpense
  -> InterestExpenseNonoperating);
- restatements (PFE 2018/2019 recast for the Upjohn discontinued operation in the 2020 10-K; AAPL
  FY2018/FY2019 diluted shares split-adjusted in the FY2020 10-K, FY2015-17 left pre-split);
- 10-Qs with both 3-month and YTD durations (cash-flow items YTD only) plus prior-year comparatives,
  including 10-Qs that pre-date the latest 10-K (MSFT);
- frames only on the latest-filed fact per period, dei cover-page share counts, 8-K / Form 4 noise in
  submissions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

OUT = Path(__file__).parent

ALIAS: dict[str, str] = {
    "rev": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "revenues": "Revenues",
    "cogs": "CostOfGoodsAndServicesSold",
    "costrev": "CostOfRevenue",
    "gp": "GrossProfit",
    "rd": "ResearchAndDevelopmentExpense",
    "sga": "SellingGeneralAndAdministrativeExpense",
    "sm": "SellingAndMarketingExpense",
    "ga": "GeneralAndAdministrativeExpense",
    "opinc": "OperatingIncomeLoss",
    "intexp": "InterestExpense",
    "pretax": "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "tax": "IncomeTaxExpenseBenefit",
    "ni": "NetIncomeLoss",
    "dil": "WeightedAverageNumberOfDilutedSharesOutstanding",
    "cash": "CashAndCashEquivalentsAtCarryingValue",
    "sti": "ShortTermInvestments",
    "mktsec": "MarketableSecuritiesCurrent",
    "cp": "CommercialPaper",
    "stb": "ShortTermBorrowings",
    "debtc": "DebtCurrent",
    "ltdc": "LongTermDebtCurrent",
    "ltdn": "LongTermDebtNoncurrent",
    "ltd": "LongTermDebt",
    "fllc": "FinanceLeaseLiabilityCurrent",
    "flln": "FinanceLeaseLiabilityNoncurrent",
    "oll": "OperatingLeaseLiability",
    "ollc": "OperatingLeaseLiabilityCurrent",
    "olln": "OperatingLeaseLiabilityNoncurrent",
    "eq": "StockholdersEquity",
    "mi": "MinorityInterest",
    "pref": "PreferredStockValue",
    "pension": "DefinedBenefitPlanFundedStatusOfPlan",
    "da": "DepreciationDepletionAndAmortization",
    "sbc": "ShareBasedCompensation",
    "capex": "PaymentsToAcquirePropertyPlantAndEquipment",
    "nwc": "IncreaseDecreaseInOperatingCapital",
    "d_ar": "IncreaseDecreaseInAccountsReceivable",
    "d_inv": "IncreaseDecreaseInInventories",
    "d_otherrec": "IncreaseDecreaseInOtherReceivables",
    "d_ap": "IncreaseDecreaseInAccountsPayable",
    "d_oa": "IncreaseDecreaseInOtherOperatingAssets",
    "d_ol": "IncreaseDecreaseInOtherOperatingLiabilities",
    "d_defrev": "IncreaseDecreaseInContractWithCustomerLiability",
    "ocf": "NetCashProvidedByUsedInOperatingActivities",
    "shout": "CommonStockSharesOutstanding",
}

INSTANT = {
    "CashAndCashEquivalentsAtCarryingValue",
    "CashAndDueFromBanks",
    "Cash",
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesCurrent",
    "CommercialPaper",
    "ShortTermBorrowings",
    "DebtCurrent",
    "LongTermDebtCurrent",
    "LongTermDebtNoncurrent",
    "LongTermDebt",
    "ConvertibleDebtNoncurrent",
    "FinanceLeaseLiabilityCurrent",
    "FinanceLeaseLiabilityNoncurrent",
    "OperatingLeaseLiability",
    "OperatingLeaseLiabilityCurrent",
    "OperatingLeaseLiabilityNoncurrent",
    "StockholdersEquity",
    "PartnersCapital",
    "MinorityInterest",
    "PreferredStockValue",
    "DefinedBenefitPlanFundedStatusOfPlan",
    "Deposits",
    "Goodwill",
    "IntangibleAssetsNetExcludingGoodwill",
    "TierOneRiskBasedCapitalToRiskWeightedAssets",
    "CommonStockSharesOutstanding",
    "RealEstateInvestmentPropertyAtCost",
    "RealEstateInvestmentPropertyAccumulatedDepreciation",
    "RealEstateInvestmentPropertyNet",
    "StandardizedMeasureOfDiscountedFutureNetCashFlowsRelatingToProvedOilAndGasReserves",
    "ProvedDevelopedAndUndevelopedReservesNet",
    "AssetsHeldInTrustNoncurrent",
    "Assets",
}
ANNUAL_ONLY = {
    "StandardizedMeasureOfDiscountedFutureNetCashFlowsRelatingToProvedOilAndGasReserves",
    "ProvedDevelopedAndUndevelopedReservesNet",
    "DefinedBenefitPlanFundedStatusOfPlan",
    "TierOneRiskBasedCapitalToRiskWeightedAssets",
}
CASH_FLOW = {
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "ShareBasedCompensation",
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsToAcquireOilAndGasPropertyAndEquipment",
    "PaymentsToAcquireRealEstate",
    "IncreaseDecreaseInOperatingCapital",
    "NetCashProvidedByUsedInOperatingActivities",
    "GainsLossesOnSalesOfInvestmentRealEstate",
} | {v for k, v in ALIAS.items() if k.startswith("d_")}
SHARE_TAGS = {
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
    "WeightedAverageLimitedPartnershipUnitsOutstandingDiluted",
    "CommonStockSharesOutstanding",
}
PURE_TAGS = {"TierOneRiskBasedCapitalToRiskWeightedAssets"}
REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "RevenuesNetOfInterestExpense",
)


def dec(first: int, last: int) -> dict[int, str]:
    return {y: f"{y}-12-31" for y in range(first, last + 1)}


@dataclass
class Q:
    fy: int
    cur: list[tuple[str, dict[str, float]]]
    prior: list[tuple[str, dict[str, float]]]
    bs: dict[str, float] = field(default_factory=dict)


@dataclass
class T:
    ticker: str
    cik: int
    name: str
    sic: str
    sic_desc: str
    fye: str
    ends: dict[int, str]
    annual: dict[str, list[float | None]]
    exchange: str = "NYSE"
    entity_type: str = "operating"
    filing_years: list[int] | None = None
    comparatives: int = 3
    tags: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    restated: dict[tuple[str, int], tuple[float, int]] = field(default_factory=dict)
    q: Q | None = None
    k_lag: int = 55
    q_lag: int = 38
    agent: str | None = None
    state: str = "DE"
    category: str = "Large Accelerated Filer"


# ---------------------------------------------------------------------------------------------------
# Ticker data (USD millions, shares millions)
# ---------------------------------------------------------------------------------------------------

AAPL = T(
    "AAPL",
    320193,
    "Apple Inc.",
    "3571",
    "Electronic Computers",
    "0928",
    ends={
        2015: "2015-09-26",
        2016: "2016-09-24",
        2017: "2017-09-30",
        2018: "2018-09-29",
        2019: "2019-09-28",
        2020: "2020-09-26",
        2021: "2021-09-25",
        2022: "2022-09-24",
        2023: "2023-09-30",
        2024: "2024-09-28",
    },
    exchange="Nasdaq",
    state="CA",
    k_lag=34,
    q_lag=34,
    annual={
        "rev": [233715, 215639, 229234, 265595, 260174, 274515, 365817, 394328, 383285, 391035],
        "cogs": [140089, 131376, 141048, 163756, 161782, 169559, 212981, 223546, 214137, 210352],
        "gp": [93626, 84263, 88186, 101839, 98392, 104956, 152836, 170782, 169148, 180683],
        "rd": [8067, 10045, 11581, 14236, 16217, 18752, 21914, 26251, 29915, 31370],
        "sga": [14329, 14194, 15261, 16705, 18245, 19916, 21973, 25094, 24932, 26097],
        "opinc": [71230, 60024, 61344, 70898, 63930, 66288, 108949, 119437, 114301, 123216],
        "intexp": [733, 1456, 2323, 3240, 3576, 2873, 2645, 2931, 3933, None],
        "pretax": [72515, 61372, 64089, 72903, 65737, 67091, 109207, 119103, 113736, 123485],
        "tax": [19121, 15685, 15738, 13372, 10481, 9680, 14527, 19300, 16741, 29749],
        "ni": [53394, 45687, 48351, 59531, 55256, 57411, 94680, 99803, 96995, 93736],
        "dil": [
            5793.069,
            5500.281,
            5251.692,
            20000.436,
            18595.652,
            17528.214,
            16864.919,
            16325.819,
            15812.547,
            15408.095,
        ],
        "cash": [21120, 20484, 20289, 25913, 48844, 38016, 34940, 23646, 29965, 29943],
        "mktsec": [20481, 46671, 53892, 40388, 51713, 52927, 27699, 24658, 31590, 35228],
        "cp": [8499, 8105, 11977, 11964, 5980, 4996, 6000, 9982, 5985, 9967],
        "ltdc": [2500, 3500, 6496, 8784, 10260, 8773, 9613, 11128, 9822, 10912],
        "ltdn": [53463, 75427, 97207, 93735, 91807, 98667, 109106, 98959, 95281, 85750],
        "ollc": [None, None, None, None, None, 1436, 1449, 1534, 1410, 1488],
        "olln": [None, None, None, None, None, 9842, 10275, 10748, 10408, 10046],
        "eq": [119355, 128249, 134047, 107147, 90488, 65339, 63090, 50672, 62146, 56950],
        "da": [11257, 10505, 10157, 10903, 12547, 11056, 11284, 11104, 11519, 11445],
        "sbc": [3586, 4210, 4840, 5340, 6068, 6829, 7906, 9038, 10833, 11688],
        "capex": [11247, 12734, 12451, 13313, 10495, 7309, 11085, 10708, 10959, 9447],
        "d_ar": [417, 527, 2093, -5322, 245, 6917, 10125, 1823, -1688, 3788],
        "d_otherrec": [3735, -1095, 4254, -8010, -2931, 1553, 3903, 7520, -1271, 1356],
        "d_inv": [238, -217, 2723, -828, -289, 127, 2642, -1484, -1618, 1046],
        "d_oa": [179, -1090, 5318, 423, -873, 9588, 8042, 6499, 5684, 11731],
        "d_ap": [5400, 1791, 9618, 9175, -1923, -4062, 12326, 9448, -1889, 6020],
        "d_ol": [4521, -1554, -1011, 38490, -4700, 8916, 7475, 6110, 3031, 15552],
        "ocf": [81266, 65824, 64225, 77434, 69391, 80674, 104038, 122151, 110543, 118254],
    },
    tags={
        "rev": [(0, "SalesRevenueNet"), (2019, "RevenueFromContractWithCustomerExcludingAssessedTax")],
        "mktsec": [(0, "AvailableForSaleSecuritiesCurrent"), (2018, "MarketableSecuritiesCurrent")],
    },
    restated={("dil", 2018): (5000.109, 2020), ("dil", 2019): (4648.913, 2020)},
    q=Q(
        fy=2025,
        cur=[
            ("2024-12-28", {"rev": 124300, "ni": 36330, "opinc": 42832, "dil": 15150.865}),
            ("2025-03-29", {"rev": 95359, "ni": 24780, "opinc": 29589, "dil": 15056.133}),
            ("2025-06-28", {"rev": 94036, "ni": 23434, "opinc": 28202, "dil": 14948.179}),
        ],
        prior=[
            ("2023-12-30", {"rev": 119575, "ni": 33916, "opinc": 40373, "dil": 15576.641}),
            ("2024-03-30", {"rev": 90753, "ni": 23636, "opinc": 27900, "dil": 15464.709}),
            ("2024-06-29", {"rev": 85777, "ni": 21448, "opinc": 25352, "dil": 15348.175}),
        ],
        bs={"cash": 36269, "mktsec": 19103, "cp": 9923, "ltdc": 10916, "ltdn": 82430, "eq": 65830},
    ),
)

MSFT = T(
    "MSFT",
    789019,
    "MICROSOFT CORP",
    "7372",
    "Services-Prepackaged Software",
    "0630",
    ends={y: f"{y}-06-30" for y in range(2019, 2026)},
    exchange="Nasdaq",
    state="WA",
    k_lag=30,
    annual={
        "rev": [125843, 143015, 168088, 198270, 211915, 245122, 281724],
        "costrev": [42910, 46078, 52232, 62650, 65863, 74114, 87831],
        "gp": [82933, 96937, 115856, 135620, 146052, 171008, 193893],
        "rd": [16876, 19269, 20716, 24512, 27195, 29510, 32488],
        "sm": [18213, 19598, 20117, 21825, 22759, 24456, 25654],
        "ga": [4885, 5111, 5107, 5900, 7575, 7609, 7223],
        "opinc": [42959, 52959, 69916, 83383, 88523, 109433, 128528],
        "intexp": [2686, 2591, 2346, 2063, 1968, 2935, 2385],
        "pretax": [43688, 53036, 71102, 83716, 89311, 107787, 123640],
        "tax": [4448, 8755, 9831, 10978, 16950, 19651, 21795],
        "ni": [39240, 44281, 61271, 72738, 72361, 88136, 101832],
        "dil": [7753, 7683, 7608, 7540, 7472, 7469, 7465],
        "cash": [11356, 13576, 14224, 13931, 34704, 18315, 30242],
        "sti": [122463, 122951, 116110, 90826, 76558, 57228, 64323],
        "ltdc": [5516, 3749, 8072, 2749, 5247, 2249, 2999],
        "ltdn": [66662, 59578, 50074, 47032, 41990, 42688, 40152],
        "fllc": [317, 540, 791, 1060, 1197, 2349, 3236],
        "flln": [5568, 7005, 11750, 14014, 22176, 32339, 47856],
        "ollc": [1409, 1581, 1685, 1909, 2409, 2796, 3100],
        "olln": [6188, 7671, 9629, 11489, 12728, 15497, 17000],
        "eq": [102330, 118304, 141988, 166542, 206223, 268477, 343479],
        "da": [11682, 12796, 11686, 14460, 13861, 22287, 34153],
        "sbc": [4652, 5289, 6118, 7502, 9611, 10734, 11974],
        "capex": [13925, 15441, 20622, 23886, 28107, 44477, 64551],
        "d_ar": [2812, 2577, 6481, 6834, 4087, 7191, 7892],
        "d_ap": [232, 3018, 2798, 2943, -2721, 3545, 569],
    },
    q=Q(
        fy=2025,
        cur=[
            ("2024-09-30", {"rev": 65585, "ni": 24667}),
            ("2024-12-31", {"rev": 69632, "ni": 24108}),
            ("2025-03-31", {"rev": 70066, "ni": 25824}),
        ],
        prior=[
            ("2023-09-30", {"rev": 56517, "ni": 22291}),
            ("2023-12-31", {"rev": 62020, "ni": 21870}),
            ("2024-03-31", {"rev": 61858, "ni": 21939}),
        ],
    ),
)

SNOW = T(
    "SNOW",
    1640147,
    "Snowflake Inc.",
    "7372",
    "Services-Prepackaged Software",
    "0131",
    ends={y: f"{y}-01-31" for y in range(2020, 2026)},
    filing_years=list(range(2021, 2026)),
    k_lag=55,
    q_lag=31,
    annual={
        "rev": [264.748, 592.049, 1219.327, 2065.659, 2806.489, 3626.396],
        "costrev": [146.0, 242.2, 453.9, 717.0, 890.6, 1218.0],
        "gp": [118.748, 349.849, 765.427, 1348.659, 1915.889, 2408.396],
        "rd": [105.4, 237.9, 466.9, 788.1, 1287.9, 1783.2],
        "sm": [293.6, 479.5, 743.9, 1106.5, 1391.4, 1654.2],
        "ga": [90.2, 176.9, 272.2, 295.7, 330.3, 427.3],
        "opinc": [-370.4, -544.8, -717.5, -842.5, -1093.4, -1456.3],
        "pretax": [-353.3, -535.3, -683.1, -796.4, -819.0, -1269.2],
        "tax": [-4.8, 3.8, -3.2, 1.1, 17.1, 16.4],
        "ni": [-348.5, -539.1, -679.9, -797.5, -836.1, -1285.6],
        "dil": [87.9, 130.8, 298.4, 317.1, 327.1, 332.7],
        "cash": [136.2, 733.5, 1085.9, 941.0, 1762.8, 2628.8],
        "sti": [297.2, 3196.1, 2891.5, 3081.6, 1473.2, 1907.5],
        "ConvertibleDebtNoncurrent": [None, None, None, None, None, 2270.4],
        "oll": [None, None, 231.6, 276.9, 254.1, 331.4],
        "eq": [1000.0, 5049.0, 5456.1, 5455.0, 5187.1, 3000.4],
        "da": [13.6, 22.2, 55.0, 144.7, 192.6, 211.4],
        "sbc": [32.7, 301.4, 605.1, 861.5, 1168.0, 1483.2],
        "capex": [42.4, 22.9, 17.9, 26.8, 34.2, 25.9],
        "d_ar": [60.2, 130.4, 318.4, 180.8, 367.8, 289.6],
        "d_defrev": [186.7, 364.3, 564.0, 731.4, 695.9, 669.0],
        "ocf": [-176.6, -45.4, 110.2, 545.6, 848.1, 959.8],
    },
    q=Q(
        fy=2026,
        cur=[("2025-04-30", {"rev": 1042.137, "opinc": -443.8, "ni": -430.1, "dil": 333.9})],
        prior=[("2024-04-30", {"rev": 828.709, "opinc": -325.7, "ni": -317.0, "dil": 334.0})],
    ),
)

JPM = T(
    "JPM",
    19617,
    "JPMORGAN CHASE & CO",
    "6021",
    "National Commercial Banks",
    "1231",
    ends=dec(2017, 2024),
    annual={
        "RevenuesNetOfInterestExpense": [99624, 108783, 115627, 119543, 121649, 128695, 158104, 177556],
        "InterestIncomeExpenseNet": [50097, 55059, 57245, 54563, 52311, 66710, 89267, 92583],
        "ProvisionForLoanLeaseAndOtherLosses": [5290, 4871, 5585, 17480, -9256, 6389, 9320, 10678],
        "NoninterestExpense": [58434, 63394, 65497, 66656, 71343, 76140, 87172, 91797],
        "intexp": [14275, 22451, 31672, 11553, 5553, 26097, 81321, 100159],
        "pretax": [35900, 40764, 44545, 35407, 59562, 46166, 61612, 75081],
        "tax": [11459, 8290, 8114, 6276, 11228, 8490, 12060, 16610],
        "ni": [24441, 32474, 36431, 29131, 48334, 37676, 49552, 58471],
        "dil": [3576.8, 3414.0, 3221.5, 3087.4, 3026.6, 2970.0, 2923.9, 2868.5],
        "Deposits": [1443982, 1470666, 1562431, 2144257, 2462303, 2340179, 2400688, 2406032],
        "CashAndDueFromBanks": [25827, 22324, 21704, 24874, 26438, 27697, 29066, 23372],
        "eq": [255693, 256515, 261330, 279354, 294127, 292332, 327878, 344758],
        "pref": [26068, 26068, 26993, 30063, 34838, 27404, 27404, 20050],
        "Goodwill": [47507, 47471, 47823, 49248, 50315, 51662, 52634, 52565],
        "IntangibleAssetsNetExcludingGoodwill": [855, 748, 819, 904, 882, 1224, 1445, 1407],
        "ltd": [284080, 282031, 291498, 281685, 301005, 295865, 391825, 401418],
        "stb": [51802, 69276, 40920, 45208, 53515, 44027, 44712, 52893],
        "TierOneRiskBasedCapitalToRiskWeightedAssets": [
            0.141,
            0.141,
            0.150,
            0.154,
            0.151,
            0.149,
            0.166,
            0.168,
        ],
        "DepreciationAmortizationAndAccretionNet": [6179, 7791, 8368, 8614, 7932, 7051, 7512, 7601],
        "sbc": [2023, 2305, 2542, 2494, 2532, 2592, 3004, 3100],
    },
    q=Q(
        fy=2025,
        cur=[
            (
                "2025-03-31",
                {
                    "RevenuesNetOfInterestExpense": 45310,
                    "InterestIncomeExpenseNet": 23273,
                    "ProvisionForLoanLeaseAndOtherLosses": 3305,
                    "ni": 14643,
                },
            ),
            (
                "2025-06-30",
                {
                    "RevenuesNetOfInterestExpense": 44912,
                    "InterestIncomeExpenseNet": 23209,
                    "ProvisionForLoanLeaseAndOtherLosses": 2849,
                    "ni": 14987,
                },
            ),
        ],
        prior=[
            (
                "2024-03-31",
                {
                    "RevenuesNetOfInterestExpense": 41934,
                    "InterestIncomeExpenseNet": 23082,
                    "ProvisionForLoanLeaseAndOtherLosses": 1884,
                    "ni": 13419,
                },
            ),
            (
                "2024-06-30",
                {
                    "RevenuesNetOfInterestExpense": 50200,
                    "InterestIncomeExpenseNet": 22746,
                    "ProvisionForLoanLeaseAndOtherLosses": 3052,
                    "ni": 18149,
                },
            ),
        ],
        bs={"Deposits": 2562580, "eq": 356924},
    ),
)

TRV = T(
    "TRV",
    86312,
    "TRAVELERS COMPANIES, INC.",
    "6331",
    "Fire, Marine & Casualty Insurance",
    "1231",
    ends=dec(2018, 2024),
    state="MN",
    annual={
        "revenues": [30282, 31581, 31981, 34816, 36884, 41364, 46423],
        "rev": [460, 465, 434, 438, 452, 469, 471],
        "PremiumsEarnedNet": [27059, 28090, 29044, 30855, 33763, 37761, 41501],
        "PolicyholderBenefitsAndClaimsIncurredNet": [18070, 19383, 19123, 20712, 22854, 26008, 27265],
        "DeferredPolicyAcquisitionCostAmortizationExpense": [4425, 4599, 4733, 5017, 5471, 6011, 6655],
        "ga": [4300, 4340, 4318, 4613, 4834, 5040, 5433],
        "intexp": [353, 357, 353, 343, 353, 392, 395],
        "pretax": [2970, 3096, 3188, 4418, 3389, 3568, 6139],
        "tax": [447, 474, 491, 756, 547, 577, 1140],
        "ni": [2523, 2622, 2697, 3662, 2842, 2991, 4999],
        "dil": [270.1, 263.9, 255.9, 250.7, 242.9, 232.8, 231.0],
        "shout": [264.0, 256.9, 252.5, 247.3, 234.0, 228.3, 227.4],
        "eq": [22894, 25943, 28440, 28887, 21560, 24003, 27864],
        "ltd": [6564, 6558, 7565, 7290, 7292, 7885, 8033],
        "Cash": [373, 494, 721, 690, 882, 741, 999],
        "da": [757, 770, 790, 812, 810, 856, 880],
        "sbc": [181, 179, 173, 180, 193, 207, 213],
    },
    q=Q(
        fy=2025,
        cur=[
            ("2025-03-31", {"revenues": 11810, "PremiumsEarnedNet": 10650, "ni": 395}),
            ("2025-06-30", {"revenues": 12146, "PremiumsEarnedNet": 10796, "ni": 1511}),
        ],
        prior=[
            ("2024-03-31", {"revenues": 11194, "PremiumsEarnedNet": 10012, "ni": 1137}),
            ("2024-06-30", {"revenues": 11294, "PremiumsEarnedNet": 10289, "ni": 534}),
        ],
    ),
)

MET = T(
    "MET",
    1099219,
    "MetLife, Inc.",
    "6311",
    "Life Insurance",
    "1231",
    ends=dec(2019, 2024),
    annual={
        "revenues": [69620, 67842, 71080, 69898, 66905, 70986],
        "PremiumsEarnedNet": [42235, 42034, 43905, 49380, 44030, 46193],
        "PolicyholderBenefitsAndClaimsIncurredNet": [44946, 45207, 48149, 53066, 47628, 49860],
        "intexp": [1091, 1000, 953, 997, 1019, 1010],
        "pretax": [7114, 6445, 7955, 2927, 1963, 5358],
        "tax": [1215, 1038, 1401, 388, 385, 932],
        "ni": [5899, 5407, 6554, 2539, 1578, 4426],
        "dil": [933.7, 909.6, 881.4, 808.4, 760.1, 717.6],
        "shout": [915.9, 891.2, 845.0, 770.0, 739.8, 692.0],
        "eq": [72582, 78920, 77440, 27191, 30029, 27445],
        "ltd": [13444, 14429, 14012, 14700, 15640, 16550],
        "cash": [16598, 19795, 21210, 20244, 20606, 20280],
    },
    q=Q(
        fy=2025,
        cur=[("2025-03-31", {"revenues": 18572, "ni": 945}), ("2025-06-30", {"revenues": 17375, "ni": 698})],
        prior=[
            ("2024-03-31", {"revenues": 17992, "ni": 867}),
            ("2024-06-30", {"revenues": 18599, "ni": 912}),
        ],
    ),
)

O_REIT = T(
    "O",
    726728,
    "REALTY INCOME CORP",
    "6798",
    "Real Estate Investment Trusts",
    "1231",
    ends=dec(2018, 2024),
    state="MD",
    annual={
        "revenues": [1327.8, 1491.6, 1651.6, 2080.5, 3343.7, 4079.0, 5271.2],
        "intexp": [290.8, 290.9, 308.9, 324.4, 465.2, 730.4, 1017.9],
        "tax": [6.4, 13.4, 13.8, 21.3, 46.3, 42.4, 66.9],
        "ni": [363.6, 436.5, 395.5, 359.5, 869.4, 872.3, 860.8],
        "dil": [291.8, 318.1, 345.9, 410.3, 598.2, 681.0, 870.3],
        "RealEstateInvestmentPropertyAtCost": [16500, 20140, 22540, 38380, 43760, 50010, 62400],
        "RealEstateInvestmentPropertyAccumulatedDepreciation": [2618, 2946, 3365, 4191, 5366, 6843, 8538],
        "RealEstateInvestmentPropertyNet": [13882, 17194, 19175, 34189, 38394, 43167, 53862],
        "GainsLossesOnSalesOfInvestmentRealEstate": [31.7, 29.9, 76.2, 55.8, 102.0, 25.0, 62.7],
        "da": [509.5, 593.3, 662.4, 897.8, 1670.0, 2016.0, 2395.3],
        "ltd": [7050, 7660, 8860, 15600, 18540, 20340, 25670],
        "eq": [9164, 11587, 12675, 22789, 26735, 30630, 38837],
        "cash": [10.4, 54.0, 824.5, 258.6, 171.1, 233.0, 445.0],
        "sbc": [20.6, 16.8, 19.3, 49.6, 26.2, 30.1, 37.1],
        "PaymentsToAcquireRealEstate": [1683, 3660, 2300, 5900, 8500, 9300, 3500],
    },
    q=Q(
        fy=2025,
        cur=[
            ("2025-03-31", {"revenues": 1380.1, "ni": 249.8}),
            ("2025-06-30", {"revenues": 1413.2, "ni": 196.9}),
        ],
        prior=[
            ("2024-03-31", {"revenues": 1260.5, "ni": 132.4}),
            ("2024-06-30", {"revenues": 1339.4, "ni": 259.4}),
        ],
    ),
)

EOG = T(
    "EOG",
    821189,
    "EOG RESOURCES INC",
    "1311",
    "Crude Petroleum & Natural Gas",
    "1231",
    ends=dec(2017, 2024),
    state="DE",
    annual={
        "revenues": [11208, 17275, 17380, 11032, 18642, 25702, 24186, 23698],
        "opinc": [1286, 4494, 3797, -514, 6114, 10166, 9665, 8093],
        "intexp": [274, 245, 185, 205, 178, 179, 148, 139],
        "pretax": [662, 4469, 3539, -740, 5932, 9901, 9663, 8162],
        "tax": [-1921, 1050, 804, -135, 1268, 2142, 2069, 1759],
        "ni": [2583, 3419, 2735, -605, 4664, 7759, 7594, 6403],
        "dil": [580.6, 584.6, 584.4, 580.8, 584.7, 588.1, 584.2, 570.6],
        "cash": [834, 1556, 2028, 3329, 5209, 5972, 5278, 7092],
        "ltdc": [356, 913, 1014, 1036, 37, 1283, 34, 534],
        "ltdn": [6030, 5170, 4160, 4990, 5072, 3795, 3765, 4220],
        "eq": [16283, 19364, 21641, 20302, 22180, 24779, 28090, 29351],
        "da": [3409, 3435, 3750, 3400, 3651, 3542, 3442, 3875],
        "sbc": [139, 143, 150, 135, 146, 150, 155, 159],
        "PaymentsToAcquireOilAndGasPropertyAndEquipment": [4227, 5827, 6152, 3388, 3796, 4607, 6054, 6026],
        "StandardizedMeasureOfDiscountedFutureNetCashFlowsRelatingToProvedOilAndGasReserves": [
            9730,
            16730,
            14890,
            8780,
            33090,
            55100,
            36480,
            33970,
        ],
        "ProvedDevelopedAndUndevelopedReservesNet|MMBbls": [1175, 1396, 1576, 1437, 1495, 1588, 1558, 1573],
        "ProvedDevelopedAndUndevelopedReservesNet|Bcf": [3730, 4440, 5022, 4797, 6383, 7630, 8203, 8650],
    },
    tags={"intexp": [(0, "InterestExpense"), (2021, "InterestExpenseNonoperating")]},
    q=Q(
        fy=2025,
        cur=[("2025-03-31", {"revenues": 5669, "ni": 1463}), ("2025-06-30", {"revenues": 5478, "ni": 1345})],
        prior=[
            ("2024-03-31", {"revenues": 6123, "ni": 1789}),
            ("2024-06-30", {"revenues": 6031, "ni": 1690}),
        ],
    ),
)

HON = T(
    "HON",
    773840,
    "Honeywell International Inc.",
    "3724",
    "Aircraft Engines & Engine Parts",
    "1231",
    ends=dec(2018, 2024),
    exchange="Nasdaq",
    annual={
        "revenues": [41802, 36709, 32637, 34392, 35466, 36662, 38498],
        "cogs": [29472, 24339, 22030, 22431, 22989, 22860, 23870],
        "sga": [6051, 5519, 4772, 4798, 5214, 5127, 5443],
        "rd": [1809, 1556, 1333, 1333, 1478, 1456, 1536],
        "intexp": [367, 357, 359, 343, 414, 765, 1058],
        "pretax": [8277, 7580, 6596, 7029, 6265, 7190, 7222],
        "tax": [1467, 1392, 1772, 1443, 1254, 1487, 1472],
        "ni": [6765, 6143, 4779, 5542, 4966, 5658, 5705],
        "dil": [754.0, 723.0, 706.0, 699.0, 682.0, 668.0, 654.0],
        "cash": [9287, 9067, 14275, 10959, 9627, 7925, 10567],
        "sti": [1623, 1349, 945, 564, 483, 170, 386],
        "stb": [3586, 3516, 3597, 3542, 2380, 2085, 4275],
        "ltdc": [2872, 1376, 2445, 1803, 3010, 1796, 2698],
        "ltdn": [9756, 11110, 16342, 14254, 15123, 16562, 25479],
        "pension": [1150, 2300, 3700, 5600, 3800, 3300, 3100],
        "eq": [18180, 18494, 17549, 18569, 16697, 15856, 18619],
        "mi": [178, 144, 167, 208, 180, 206, 434],
        "da": [1115, 1093, 1114, 1223, 1204, 1176, 1327],
        "sbc": [175, 153, 217, 217, 202, 202, 213],
        "capex": [828, 839, 906, 895, 766, 1039, 1164],
        "nwc": [380, -130, -720, 380, 870, 1290, 1140],
    },
    q=Q(
        fy=2025,
        cur=[("2025-03-31", {"revenues": 9822, "ni": 1450}), ("2025-06-30", {"revenues": 10352, "ni": 1570})],
        prior=[
            ("2024-03-31", {"revenues": 9105, "ni": 1468}),
            ("2024-06-30", {"revenues": 9578, "ni": 1544}),
        ],
    ),
)

PFE = T(
    "PFE",
    78003,
    "PFIZER INC",
    "2834",
    "Pharmaceutical Preparations",
    "1231",
    ends=dec(2017, 2024),
    annual={
        "revenues": [52546, 40825, 41172, 41908, 81288, 100330, 58496, 63627],
        "cogs": [11240, 8054, 8061, 8692, 30821, 34344, 24954, 17851],
        "sga": [14784, 12612, 12726, 11597, 12703, 13677, 14771, 14730],
        "rd": [7657, 7760, 8385, 9405, 13829, 11428, 10679, 10822],
        "intexp": [1270, 1275, 1437, 1449, 1291, 1238, 2209, 3091],
        "pretax": [12305, 9550, 10344, 7036, 24311, 34729, 1058, 8023],
        "tax": [-9049, 706, 1384, 477, 1852, 3328, -1115, -28],
        "ni": [21308, 11153, 16273, 9616, 21979, 31372, 2119, 8031],
        "dil": [6050, 5977, 5675, 5632, 5708, 5733, 5709, 5710],
        "cash": [1342, 1139, 1121, 1786, 1944, 416, 2853, 1043],
        "sti": [18650, 17694, 8525, 10437, 29125, 22316, 9837, 19434],
        "debtc": [9953, 8831, 16195, 2703, 2241, 2945, 10350, 6946],
        "ltdn": [33538, 32909, 35955, 37133, 36195, 32884, 61538, 57020],
        "pension": [-6600, -5400, -4700, -4000, -2000, -1300, -1500, -1100],
        "eq": [71308, 63407, 63143, 63238, 77201, 95661, 89014, 88203],
        "mi": [351, 350, 303, 253, 262, 256, 266, 293],
        "da": [6269, 6384, 6010, 4777, 5191, 5064, 5395, 6094],
        "sbc": [840, 949, 687, 755, 1182, 1212, 1121, 1150],
        "capex": [1956, 2042, 2176, 2252, 2711, 3236, 3907, 2909],
        "ocf": [16802, 15827, 12588, 14403, 32580, 29267, 8700, 12744],
    },
    restated={
        ("revenues", 2018): (53647, 2020),
        ("revenues", 2019): (51750, 2020),
        ("cogs", 2018): (11248, 2020),
        ("cogs", 2019): (10219, 2020),
        ("sga", 2018): (14455, 2020),
        ("sga", 2019): (14350, 2020),
        ("rd", 2018): (8006, 2020),
        ("rd", 2019): (8650, 2020),
        ("intexp", 2018): (1316, 2020),
        ("intexp", 2019): (1574, 2020),
        ("pretax", 2018): (11885, 2020),
        ("pretax", 2019): (11481, 2020),
    },
    q=Q(
        fy=2025,
        cur=[
            ("2025-03-30", {"revenues": 13715, "ni": 2966}),
            ("2025-06-29", {"revenues": 14653, "ni": 2909}),
        ],
        prior=[
            ("2024-03-31", {"revenues": 14879, "ni": 3115}),
            ("2024-06-30", {"revenues": 13283, "ni": 41}),
        ],
    ),
)

VKTX = T(
    "VKTX",
    1607678,
    "Viking Therapeutics, Inc.",
    "2834",
    "Pharmaceutical Preparations",
    "1231",
    ends=dec(2018, 2024),
    exchange="Nasdaq",
    annual={
        "rd": [17.4, 24.4, 32.4, 44.9, 54.3, 77.6, 101.9],
        "ga": [5.4, 7.7, 9.5, 12.6, 14.5, 32.0, 41.6],
        "opinc": [-22.8, -32.1, -41.9, -57.5, -68.8, -109.6, -143.5],
        "InvestmentIncomeInterest": [0.9, 6.2, 2.4, 2.7, -1.1, 23.7, 33.5],
        "pretax": [-21.9, -25.9, -39.5, -54.8, -69.9, -85.9, -110.0],
        "ni": [-21.9, -25.9, -39.5, -54.8, -69.9, -85.9, -110.0],
        "dil": [71.6, 72.4, 72.9, 78.2, 79.7, 99.1, 107.6],
        "cash": [26.4, 32.4, 21.1, 45.5, 30.0, 51.0, 88.5],
        "sti": [275.1, 240.3, 218.5, 163.4, 125.3, 311.3, 814.6],
        "eq": [296.2, 272.2, 240.6, 213.3, 161.2, 362.0, 903.1],
        "sbc": [3.9, 6.0, 7.5, 9.6, 10.9, 23.6, 32.3],
        "ocf": [-15.9, -21.5, -31.9, -41.6, -59.4, -70.1, -88.9],
    },
    q=Q(
        fy=2025,
        cur=[
            ("2025-03-31", {"rd": 36.4, "opinc": -45.6 - 8.5, "ni": -45.6}),
            ("2025-06-30", {"rd": 65.6, "opinc": -65.6 - 9.0, "ni": -65.6}),
        ],
        prior=[
            ("2024-03-31", {"rd": 23.4, "opinc": -27.3 - 9.4, "ni": -27.3}),
            ("2024-06-30", {"rd": 22.8, "opinc": -21.3 - 9.7, "ni": -21.3}),
        ],
    ),
)

ALAB = T(
    "ALAB",
    1736297,
    "Astera Labs, Inc.",
    "3674",
    "Semiconductors & Related Devices",
    "1231",
    ends=dec(2023, 2024),
    filing_years=[2024],
    comparatives=2,
    exchange="Nasdaq",
    category="Non-accelerated Filer",
    annual={
        "rev": [115.8, 396.3],
        "costrev": [35.8, 93.6],
        "gp": [80.0, 302.7],
        "rd": [68.4, 196.6],
        "sm": [22.1, 93.0],
        "ga": [20.5, 126.2],
        "opinc": [-31.0, -113.1],
        "pretax": [-26.9, -86.1],
        "tax": [-0.6, -2.7],
        "ni": [-26.3, -83.4],
        "dil": [55.6, 142.1],
        "cash": [51.6, 79.6],
        "sti": [108.7, 834.3],
        "eq": [196.9, 1000.4],
        "sbc": [13.1, 213.9],
        "capex": [5.3, 26.1],
    },
    q=Q(
        fy=2025,
        cur=[
            ("2025-03-31", {"rev": 159.4, "ni": 31.8, "opinc": 15.2, "dil": 176.4}),
            ("2025-06-30", {"rev": 191.9, "ni": 51.2, "opinc": 39.8, "dil": 178.0}),
        ],
        prior=[
            ("2024-03-31", {"rev": 65.3, "ni": -93.0, "opinc": -99.9, "dil": 118.3}),
            ("2024-06-30", {"rev": 76.9, "ni": -7.5, "opinc": -17.1, "dil": 153.7}),
        ],
    ),
)

CVII = T(
    "CVII",
    1838803,
    "Churchill Capital Corp VII",
    "6770",
    "Blank Checks",
    "1231",
    ends=dec(2021, 2024),
    category="Non-accelerated Filer",
    annual={
        "opinc": [-4.1, -3.2, -4.6, -5.9],
        "InvestmentIncomeInterest": [0.1, 19.6, 44.3, 31.2],
        "pretax": [35.2, 58.1, 39.7, 25.3],
        "tax": [0.0, 3.8, 9.2, 6.4],
        "ni": [35.2, 54.3, 30.5, 18.9],
        "dil": [138.0, 138.0, 70.3, 58.2],
        "AssetsHeldInTrustNoncurrent": [1380.2, 1398.9, 598.1, 612.4],
        "cash": [1.6, 0.4, 0.1, 0.1],
        "eq": [-48.3, -60.2, -74.1, -83.0],
    },
    q=Q(
        fy=2025,
        cur=[("2025-03-31", {"ni": 4.1}), ("2025-06-30", {"ni": 3.8})],
        prior=[("2024-03-31", {"ni": 5.2}), ("2024-06-30", {"ni": 4.9})],
    ),
)

EPD = T(
    "EPD",
    1061219,
    "ENTERPRISE PRODUCTS PARTNERS L.P.",
    "4922",
    "Natural Gas Transmission",
    "1231",
    ends=dec(2018, 2024),
    state="DE",
    annual={
        "revenues": [36534, 32789, 27200, 40807, 58186, 49715, 56219],
        "opinc": [5521, 6103, 5031, 6108, 7158, 7036, 7367],
        "intexp": [1097, 1243, 1287, 1283, 1244, 1269, 1347],
        "pretax": [4322, 4650, 3770, 4826, 5697, 5675, 6066],
        "tax": [45, -45, -124, 70, 82, 44, 65],
        "ni": [4172, 4591, 3776, 4638, 5490, 5532, 5901],
        "WeightedAverageLimitedPartnershipUnitsOutstandingDiluted": [
            2185,
            2197,
            2203,
            2203,
            2198,
            2196,
            2195,
        ],
        "PartnersCapital": [24122, 25285, 24643, 25563, 26622, 27864, 28583],
        "mi": [404, 1219, 1284, 1232, 1109, 1060, 1105],
        "cash": [345, 410, 1061, 316, 76, 180, 583],
        "ltdc": [2081, 1500, 1300, 1400, 1300, 1150, 1150],
        "ltdn": [24125, 26318, 29295, 28135, 27995, 27400, 30600],
        "da": [1791, 1949, 2061, 2146, 2187, 2328, 2435],
        "sbc": [162, 180, 191, 202, 214, 223, 238],
        "capex": [4223, 4531, 3288, 1923, 1653, 3265, 4544],
    },
    q=Q(
        fy=2025,
        cur=[
            ("2025-03-31", {"revenues": 15418, "ni": 1395}),
            ("2025-06-30", {"revenues": 11357, "ni": 1358}),
        ],
        prior=[
            ("2024-03-31", {"revenues": 14757, "ni": 1469}),
            ("2024-06-30", {"revenues": 13475, "ni": 1406}),
        ],
    ),
)

NEM = T(
    "NEM",
    1164727,
    "NEWMONT Corp /DE/",
    "1040",
    "Gold and Silver Ores",
    "1231",
    ends=dec(2018, 2024),
    annual={
        "rev": [7253, 9740, 11497, 12222, 11915, 11812, 18682],
        "cogs": [4093, 5195, 5014, 5435, 6207, 6670, 8337],
        "intexp": [207, 301, 308, 274, 227, 243, 424],
        "pretax": [773, 3694, 3540, 2332, -368, -2478, 4431],
        "tax": [386, 832, 704, 1098, 3, -48, 1033],
        "ni": [341, 2805, 2829, 1166, -429, -2494, 3348],
        "dil": [534, 736, 805, 800, 794, 868, 1153],
        "cash": [3397, 2243, 5540, 4992, 2877, 3002, 3619],
        "ltdc": [626, 0, 551, 0, 0, 0, 924],
        "ltdn": [4044, 6138, 6031, 5565, 5571, 8874, 7996],
        "eq": [10535, 21420, 23008, 22022, 19331, 29020, 29868],
        "mi": [959, 1121, 1136, 1175, 1112, 1118, 1150],
        "da": [1215, 1960, 2300, 2323, 2185, 2111, 2604],
        "sbc": [89, 97, 76, 87, 76, 88, 110],
        "capex": [1032, 1463, 1302, 1653, 2131, 2666, 3358],
    },
    q=Q(
        fy=2025,
        cur=[("2025-03-31", {"rev": 5010, "ni": 1891}), ("2025-06-30", {"rev": 5317, "ni": 2062})],
        prior=[("2024-03-31", {"rev": 4023, "ni": 170}), ("2024-06-30", {"rev": 4402, "ni": 853})],
    ),
)

DUK = T(
    "DUK",
    1326160,
    "Duke Energy CORP",
    "4931",
    "Electric & Other Services Combined",
    "1231",
    ends=dec(2017, 2024),
    state="DE",
    annual={
        "revenues": [23565, 24521, 25079, 23868, 25097, 28768, 29060, 30357],
        "opinc": [6320, 5944, 6826, 6127, 6382, 6904, 7556, 8386],
        "intexp": [2136, 2094, 2204, 2162, 2207, 2439, 3014, 3366],
        "pretax": [4255, 3114, 4226, 1141, 4356, 2850, 3431, 4991],
        "tax": [1196, 448, 519, -236, 448, 300, 590, 589],
        "ni": [3059, 2666, 3707, 1377, 3908, 2550, 2841, 4402],
        "dil": [700, 708, 729, 736, 769, 770, 771, 772],
        "cash": [358, 442, 311, 259, 343, 409, 253, 314],
        "stb": [2788, 3410, 3135, 2873, 5337, 4600, 4000, 4700],
        "ltdc": [3244, 3406, 3141, 4238, 3387, 3878, 4348, 5293],
        "ltdn": [49035, 51123, 54985, 55625, 60448, 67061, 72452, 76340],
        "fllc": [None, None, 101, 111, 120, 127, 131, 139],
        "flln": [None, None, 919, 1006, 1030, 1053, 1077, 1099],
        "ollc": [None, None, 244, 233, 225, 223, 220, 218],
        "olln": [None, None, 1340, 1340, 1208, 1154, 1100, 1050],
        "eq": [41739, 43834, 46822, 47964, 49329, 49276, 49322, 50133],
        "pref": [0, 0, 973, 1962, 1962, 1962, 1962, 973],
        "mi": [5, 4, 1125, 1011, 1947, 2466, 2730, 2982],
        "pension": [-1070, -1302, -830, -498, -74, -311, -283, -150],
        "DepreciationAmortizationAndAccretionNet": [3527, 3746, 4548, 4747, 4811, 5176, 5512, 5864],
        "PaymentsToAcquireProductiveAssets": [8052, 9668, 11672, 10160, 9752, 11367, 12640, 12316],
        "sbc": [32, 44, 45, 50, 45, 52, 58, 61],
    },
    q=Q(
        fy=2025,
        cur=[("2025-03-31", {"revenues": 8248, "ni": 1369}), ("2025-06-30", {"revenues": 7502, "ni": 1034})],
        prior=[("2024-03-31", {"revenues": 7669, "ni": 1127}), ("2024-06-30", {"revenues": 7172, "ni": 900})],
    ),
)

TICKERS: list[T] = [AAPL, MSFT, SNOW, JPM, TRV, MET, O_REIT, EOG, HON, PFE, VKTX, ALAB, CVII, EPD, NEM, DUK]


# ---------------------------------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------------------------------


def _tag_and_unit(key: str, t: T, filing_fy: int) -> tuple[str, str]:
    base, _, unit = key.partition("|")
    tag = ALIAS.get(base, base)
    for from_fy, override in sorted(t.tags.get(base, [])):
        if filing_fy >= from_fy:
            tag = override
    if not unit:
        unit = "shares" if tag in SHARE_TAGS or base == "dil" else ("pure" if tag in PURE_TAGS else "USD")
    return tag, unit


def _scale(val: float, unit: str) -> float | int:
    if unit in ("USD", "shares"):
        return int(round(val * 1_000_000))
    if unit == "pure":
        return round(val, 4)
    return val


def _frame(start: date | None, end: date) -> str:
    if start is None:
        return f"CY{end.year}Q{(end.month - 1) // 3 + 1}I"
    days = (end - start).days
    if days >= 350:
        return f"CY{(end - timedelta(days=182)).year}"
    mid = start + timedelta(days=days // 2)
    return f"CY{mid.year}Q{(mid.month - 1) // 3 + 1}"


class Builder:
    def __init__(self, t: T):
        self.t = t
        self.years = sorted(t.ends)
        for key, vals in t.annual.items():
            assert len(vals) == len(self.years), (t.ticker, key, len(vals))
        self.facts: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.dei: list[dict[str, Any]] = []
        self.filings: list[dict[str, Any]] = []
        self.seq = 0
        self.agent = t.agent or f"{t.cik:010d}"
        rev_keys = [k for k in t.annual if ALIAS.get(k, k) in REVENUE_TAGS]
        # the "real" top line is the largest revenue series (TRV: Revenues, not the fee-income subset)
        self.rev_key = max(rev_keys, key=lambda k: t.annual[k][-1] or 0) if rev_keys else None

    # -- helpers -----------------------------------------------------------------------------------
    def end(self, fy: int) -> date:
        return date.fromisoformat(self.t.ends[fy])

    def start(self, fy: int) -> date:
        if fy - 1 in self.t.ends:
            return self.end(fy - 1) + timedelta(days=1)
        e = self.end(fy)
        return date(e.year - 1, e.month, min(e.day, 28)) + timedelta(days=1)

    def annual(self, key: str, fy: int, filing_fy: int | None = None) -> float | None:
        if fy not in self.t.ends:
            return None
        if filing_fy is not None and (key, fy) in self.t.restated:
            orig, restating_fy = self.t.restated[(key, fy)]
            if filing_fy < restating_fy:
                return orig
        return self.t.annual[key][self.years.index(fy)]

    def accn(self, filed: date) -> str:
        self.seq += 1
        return f"{self.agent}-{filed.year % 100:02d}-{self.seq:06d}"

    def add(
        self,
        key: str,
        filing_fy: int,
        fp: str,
        form: str,
        accn: str,
        filed: date,
        start: date | None,
        end: date,
        val: float,
    ) -> None:
        tag, unit = _tag_and_unit(key, self.t, filing_fy)
        fact: dict[str, Any] = {}
        if start is not None:
            fact["start"] = start.isoformat()
        fact.update(
            end=end.isoformat(),
            val=_scale(val, unit),
            accn=accn,
            fy=filing_fy,
            fp=fp,
            form=form,
            filed=filed.isoformat(),
        )
        self.facts.setdefault((tag, unit), []).append(fact)

    def filing(self, form: str, filed: date, report: date, accn: str, fy: int, fp: str) -> None:
        self.filings.append(
            dict(
                accessionNumber=accn,
                filingDate=filed.isoformat(),
                reportDate=report.isoformat(),
                form=form,
                primaryDocument=f"{self.t.ticker.lower()}-{report:%Y%m%d}.htm",
                primaryDocDescription=form,
                isXBRL=1,
                isInlineXBRL=1,
            )
        )
        dil = self.t.annual.get("dil") or next((v for k, v in self.t.annual.items() if "Units" in k), None)
        if dil:
            last = [v for v in dil if v][-1]
            self.dei.append(
                dict(
                    end=(filed - timedelta(days=12)).isoformat(),
                    val=int(last * 0.99e6),
                    accn=accn,
                    fy=fy,
                    fp=fp,
                    form=form,
                    filed=filed.isoformat(),
                )
            )

    # -- 10-K --------------------------------------------------------------------------------------
    def build_10k(self, fy: int) -> None:
        t = self.t
        e = self.end(fy)
        filed = e + timedelta(days=t.k_lag)
        accn = self.accn(filed)
        self.filing("10-K", filed, e, accn, fy, "FY")
        for key in t.annual:
            tag, _ = _tag_and_unit(key, t, fy)
            if tag in INSTANT:
                years = [fy, fy - 1]
            else:
                years = [fy - i for i in range(t.comparatives)]
            if tag in ANNUAL_ONLY or tag in INSTANT:
                years = years[:2]
            for y in years:
                val = self.annual(key, y, fy)
                if val is None:
                    continue
                if tag in INSTANT:
                    self.add(key, fy, "FY", "10-K", accn, filed, None, self.end(y), val)
                else:
                    self.add(key, fy, "FY", "10-K", accn, filed, self.start(y), self.end(y), val)

    # -- 10-Q --------------------------------------------------------------------------------------
    def quarter_values(
        self, key: str, rows: list[tuple[str, dict[str, float]]], fy: int
    ) -> list[float] | None:
        """Per-quarter values for `key` in fiscal year `fy` (explicit, else derived from annual ratios)."""
        ann_fy = fy if fy in self.t.ends and self.annual(key, fy) is not None else fy - 1
        annual = self.annual(key, ann_fy)
        tag, unit = _tag_and_unit(key, self.t, fy)
        out = []
        for _end, vals in rows:
            if key in vals:
                out.append(vals[key])
                continue
            if annual is None:
                return None
            if unit == "shares":
                out.append(annual)
            elif self.rev_key and self.rev_key in vals and self.annual(self.rev_key, ann_fy):
                out.append(annual * vals[self.rev_key] / self.annual(self.rev_key, ann_fy))
            else:
                out.append(annual / 4)
        return out

    def build_10qs(self) -> None:
        t, q = self.t, self.t.q
        assert q is not None
        fy_start, prior_start = (
            self.start(q.fy) if q.fy in t.ends else self.end(q.fy - 1) + timedelta(days=1),
            self.start(q.fy - 1),
        )
        for i, (cur_end_s, _vals) in enumerate(q.cur):
            cur_end = date.fromisoformat(cur_end_s)
            prior_end = date.fromisoformat(q.prior[i][0])
            filed = cur_end + timedelta(days=t.q_lag)
            fp = f"Q{i + 1}"
            accn = self.accn(filed)
            self.filing("10-Q", filed, cur_end, accn, q.fy, fp)
            q_start = fy_start if i == 0 else date.fromisoformat(q.cur[i - 1][0]) + timedelta(days=1)
            pq_start = prior_start if i == 0 else date.fromisoformat(q.prior[i - 1][0]) + timedelta(days=1)
            for key in t.annual:
                tag, unit = _tag_and_unit(key, t, q.fy)
                if tag in ANNUAL_ONLY:
                    continue
                if tag in INSTANT:
                    last_fy = q.fy - 1
                    fy_end_val = self.annual(key, last_fy)
                    if fy_end_val is None:
                        continue
                    cur_val = q.bs.get(key, fy_end_val) if i == len(q.cur) - 1 else fy_end_val
                    self.add(key, q.fy, fp, "10-Q", accn, filed, None, cur_end, cur_val)
                    self.add(key, q.fy, fp, "10-Q", accn, filed, None, self.end(last_fy), fy_end_val)
                    continue
                cur_q = self.quarter_values(key, q.cur, q.fy)
                prior_q = self.quarter_values(key, q.prior, q.fy - 1)
                if cur_q is None or prior_q is None:
                    continue
                shares = unit == "shares"

                def ytd(vals: list[float], n: int, _shares: bool = shares) -> float:
                    part = vals[: n + 1]
                    return sum(part) / len(part) if _shares else sum(part)

                if tag not in CASH_FLOW:
                    self.add(key, q.fy, fp, "10-Q", accn, filed, q_start, cur_end, cur_q[i])
                    self.add(key, q.fy, fp, "10-Q", accn, filed, pq_start, prior_end, prior_q[i])
                if i > 0 or tag in CASH_FLOW:
                    self.add(key, q.fy, fp, "10-Q", accn, filed, fy_start, cur_end, ytd(cur_q, i))
                    self.add(key, q.fy, fp, "10-Q", accn, filed, prior_start, prior_end, ytd(prior_q, i))

    # -- output ------------------------------------------------------------------------------------
    def build(self) -> tuple[dict[str, Any], dict[str, Any]]:
        t = self.t
        events: list[tuple[date, str, int]] = []
        for fy in t.filing_years or self.years:
            events.append((self.end(fy) + timedelta(days=t.k_lag), "10-K", fy))
        if t.q:
            events.append((date.fromisoformat(t.q.cur[0][0]), "10-Q", t.q.fy))
        for _when, kind, fy in sorted(events):
            if kind == "10-K":
                self.build_10k(fy)
            else:
                self.build_10qs()

        us_gaap: dict[str, Any] = {}
        for (tag, unit), facts in sorted(self.facts.items()):
            latest: dict[tuple[str | None, str], dict[str, Any]] = {}
            for f in facts:
                k = (f.get("start"), f["end"])
                if k not in latest or f["filed"] >= latest[k]["filed"]:
                    latest[k] = f
            for (s, e), f in latest.items():
                f["frame"] = _frame(date.fromisoformat(s) if s else None, date.fromisoformat(e))
            facts.sort(key=lambda f: (f["end"], f.get("start") or "", f["filed"]))
            entry = us_gaap.setdefault(tag, {"label": tag, "description": f"{tag} (fixture)", "units": {}})
            entry["units"][unit] = facts
        companyfacts = {
            "cik": t.cik,
            "entityName": t.name,
            "facts": {
                "dei": {
                    "EntityCommonStockSharesOutstanding": {
                        "label": "Entity Common Stock, Shares Outstanding",
                        "description": "Cover page shares outstanding.",
                        "units": {"shares": self.dei},
                    }
                },
                "us-gaap": us_gaap,
            },
        }
        return companyfacts, self.submissions()

    def submissions(self) -> dict[str, Any]:
        t = self.t
        rows = list(self.filings)
        for f in self.filings:  # earnings 8-Ks and insider Form 4s as noise
            filed = date.fromisoformat(f["filingDate"])
            for form, delta in (("8-K", -4), ("4", 9)):
                when = filed + timedelta(days=delta)
                rows.append(
                    dict(
                        accessionNumber=self.accn(when),
                        filingDate=when.isoformat(),
                        reportDate=when.isoformat() if form == "8-K" else "",
                        form=form,
                        primaryDocument=f"{t.ticker.lower()}-{when:%Y%m%d}{'x8k' if form == '8-K' else 'f4'}.htm",
                        primaryDocDescription=form,
                        isXBRL=1 if form == "8-K" else 0,
                        isInlineXBRL=0,
                    )
                )
        return make_submissions(
            t.cik,
            t.name,
            [t.ticker],
            [t.exchange],
            t.sic,
            t.sic_desc,
            t.fye,
            t.entity_type,
            t.state,
            t.category,
            rows,
        )


def make_submissions(
    cik: int,
    name: str,
    tickers: list[str],
    exchanges: list[str],
    sic: str,
    sic_desc: str,
    fye: str,
    entity_type: str,
    state: str,
    category: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    rows = sorted(rows, key=lambda r: (r["filingDate"], r["accessionNumber"]), reverse=True)
    keys = [
        "accessionNumber",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "act",
        "form",
        "fileNumber",
        "filmNumber",
        "items",
        "size",
        "isXBRL",
        "isInlineXBRL",
        "primaryDocument",
        "primaryDocDescription",
    ]
    recent: dict[str, list[Any]] = {k: [] for k in keys}
    for r in rows:
        r = dict(r)
        r.setdefault("acceptanceDateTime", f"{r['filingDate']}T16:05:12.000Z")
        r.setdefault("act", "34" if r["form"] != "4" else "")
        r.setdefault("fileNumber", "001-00000" if r["form"] != "4" else "")
        r.setdefault("filmNumber", "")
        r.setdefault("items", "2.02,9.01" if r["form"] == "8-K" else "")
        r.setdefault("size", 1_000_000 if r["form"] in ("10-K", "20-F") else 250_000)
        for k in keys:
            recent[k].append(r.get(k, ""))
    return {
        "cik": str(cik),
        "entityType": entity_type,
        "sic": sic,
        "sicDescription": sic_desc,
        "ownerOrg": "",
        "insiderTransactionForOwnerExists": 0,
        "insiderTransactionForIssuerExists": 1,
        "name": name,
        "tickers": tickers,
        "exchanges": exchanges,
        "ein": "",
        "description": "",
        "website": "",
        "investorWebsite": "",
        "category": category,
        "fiscalYearEnd": fye,
        "stateOfIncorporation": state,
        "stateOfIncorporationDescription": state,
        "addresses": {},
        "phone": "",
        "flags": "",
        "formerNames": [],
        "filings": {"recent": recent, "files": []},
    }


def tsm_submissions() -> dict[str, Any]:
    """Foreign private issuer: 20-F annual reports + 6-K current reports, no 10-K/10-Q."""
    rows = []
    seq = 0
    for year in range(2018, 2025):
        filed = date(year + 1, 4, 16)
        seq += 1
        rows.append(
            dict(
                accessionNumber=f"0001046179-{(year + 1) % 100:02d}-{seq:06d}",
                filingDate=filed.isoformat(),
                reportDate=f"{year}-12-31",
                form="20-F",
                primaryDocument=f"tsm-20{year % 100:02d}1231.htm",
                primaryDocDescription="20-F",
                isXBRL=1,
                isInlineXBRL=1,
            )
        )
        for month in (1, 4, 7, 10):
            seq += 1
            when = date(year + 1 if month == 1 else year, month, 17)
            rows.append(
                dict(
                    accessionNumber=f"0001046179-{when.year % 100:02d}-{seq:06d}",
                    filingDate=when.isoformat(),
                    reportDate=when.isoformat(),
                    form="6-K",
                    primaryDocument=f"tsm6k{when:%Y%m}.htm",
                    primaryDocDescription="6-K",
                    isXBRL=0,
                    isInlineXBRL=0,
                )
            )
    return make_submissions(
        1046179,
        "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD",
        ["TSM"],
        ["NYSE"],
        "3674",
        "Semiconductors & Related Devices",
        "1231",
        "operating",
        "F5",
        "Large Accelerated Filer",
        rows,
    )


COMPANY_TICKERS = [
    (320193, "AAPL", "Apple Inc."),
    (789019, "MSFT", "MICROSOFT CORP"),
    (1652044, "GOOGL", "Alphabet Inc."),
    (1652044, "GOOG", "Alphabet Inc."),
    (1067983, "BRK-B", "BERKSHIRE HATHAWAY INC"),
    (19617, "JPM", "JPMORGAN CHASE & CO"),
    (86312, "TRV", "TRAVELERS COMPANIES, INC."),
    (1099219, "MET", "MetLife, Inc."),
    (726728, "O", "REALTY INCOME CORP"),
    (821189, "EOG", "EOG RESOURCES INC"),
    (773840, "HON", "Honeywell International Inc."),
    (78003, "PFE", "PFIZER INC"),
    (1640147, "SNOW", "Snowflake Inc."),
    (1607678, "VKTX", "Viking Therapeutics, Inc."),
    (1736297, "ALAB", "Astera Labs, Inc."),
    (1838803, "CVII", "Churchill Capital Corp VII"),
    (1061219, "EPD", "ENTERPRISE PRODUCTS PARTNERS L.P."),
    (1164727, "NEM", "NEWMONT Corp /DE/"),
    (1326160, "DUK", "Duke Energy CORP"),
    (1046179, "TSM", "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"),
]


def main() -> None:
    def dump(name: str, obj: Any) -> None:
        (OUT / name).write_text(json.dumps(obj, indent=1) + "\n")

    for t in TICKERS:
        cf, sub = Builder(t).build()
        dump(f"{t.ticker}_companyfacts.json", cf)
        dump(f"{t.ticker}_submissions.json", sub)
    dump("TSM_submissions.json", tsm_submissions())
    dump(
        "company_tickers.json",
        {
            str(i): {"cik_str": c, "ticker": tk, "title": title}
            for i, (c, tk, title) in enumerate(COMPANY_TICKERS)
        },
    )


if __name__ == "__main__":
    main()
