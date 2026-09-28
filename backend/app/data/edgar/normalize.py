"""XBRL tag mapping, TTM, restatements (spec §5.2).

`normalize(companyfacts, submissions, ticker)` turns SEC `companyfacts` + `submissions` JSON into
`NormalizedFinancials`:

- **Tag mapping**: `TAG_MAP` maps each concept to an ordered tuple of XBRL tags; per period the first
  tag with a value wins, so tag switches over time (e.g. `SalesRevenueNet` ->
  `RevenueFromContractWithCustomerExcludingAssessedTax`) are handled period by period.
- **Restatements**: for the same tag/unit/period, the fact with the latest `filed` date wins.
- **Annual rows**: fiscal-year durations (350-380 days) reported in a 10-K, oldest -> newest.
- **TTM row** (always last): latest annual + latest YTD (10-Q) - prior-year same YTD. Works for any
  fiscal-year-end month because periods are matched on dates, not calendar quarters. When there is no
  10-Q after the latest 10-K the TTM row equals the latest fiscal year (flagged).
- Every unmapped / defaulted / derived / proxy value is recorded in `data_confidence_flags`.

Units: money raw USD, shares raw count, ratios decimals.

Also exposes small helpers the classifier needs (`present_tags`, `latest_periodic_accession`,
`filer_forms`, `entity_type`, `sic`, `fiscal_year_end_month`, ...).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import cache
from typing import Any, NamedTuple

from app.schemas.financials import (
    BalanceSheetLine,
    BankSpecificLine,
    CashFlowLine,
    EpSpecificLine,
    FiscalPeriod,
    IncomeStatementLine,
    InsurerSpecificLine,
    NormalizedFinancials,
    ReitSpecificLine,
)

PERIODIC_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A", "10-Q", "10-Q/A"})
ANNUAL_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A"})
QUARTERLY_FORMS = frozenset({"10-Q", "10-Q/A"})

ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380
REVENUE_JUMP_THRESHOLD = 0.5  # |YoY revenue change| above this gets a flag (M&A? restatement?)
MIN_YEARS_WITHOUT_FLAG = 5

USD = "USD"
SHARES = "shares"
PURE = "pure"

# concept -> ordered list of us-gaap tags to try (first with a value for the period wins)
TAG_MAP: dict[str, tuple[str, ...]] = {
    # income statement
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueGoodsNet",
        "RevenuesNetOfInterestExpense",
    ),
    "cogs": (
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
        "CostOfServices",
    ),
    "gross_profit": ("GrossProfit",),
    "sga": ("SellingGeneralAndAdministrativeExpense",),
    "selling_marketing": ("SellingAndMarketingExpense", "SellingExpense"),
    "general_admin": ("GeneralAndAdministrativeExpense",),
    "rd": (
        "ResearchAndDevelopmentExpense",
        "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    ),
    "operating_income": ("OperatingIncomeLoss",),
    "interest_expense": (
        "InterestExpenseNonoperating",
        "InterestExpenseDebt",
        "InterestExpense",
        "InterestAndDebtExpense",
    ),
    "pretax_income": (
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic",
    ),
    "tax_expense": ("IncomeTaxExpenseBenefit",),
    "net_income": (
        "NetIncomeLoss",
        "NetIncomeLossAvailableToCommonStockholdersBasic",
        "ProfitLoss",
    ),
    "diluted_shares": (
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageLimitedPartnershipUnitsOutstandingDiluted",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageLimitedPartnershipUnitsOutstanding",
    ),
    # balance sheet
    "cash": (
        "CashAndCashEquivalentsAtCarryingValue",
        "CashAndDueFromBanks",
        "Cash",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ),
    "short_term_investments": (
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
        "AvailableForSaleSecuritiesCurrent",
    ),
    "debt_current": ("DebtCurrent",),
    "ltd_current": ("LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
    "commercial_paper": ("CommercialPaper",),
    "short_term_borrowings": ("ShortTermBorrowings", "OtherShortTermBorrowings"),
    "ltd_noncurrent": (
        "LongTermDebtNoncurrent",
        "LongTermDebtAndCapitalLeaseObligations",
        "ConvertibleDebtNoncurrent",
        "ConvertibleNotesPayable",
    ),
    "ltd_total": ("LongTermDebt",),
    "finance_lease_total": ("FinanceLeaseLiability",),
    "finance_lease_current": ("FinanceLeaseLiabilityCurrent",),
    "finance_lease_noncurrent": ("FinanceLeaseLiabilityNoncurrent",),
    "operating_lease_total": ("OperatingLeaseLiability",),
    "operating_lease_current": ("OperatingLeaseLiabilityCurrent",),
    "operating_lease_noncurrent": ("OperatingLeaseLiabilityNoncurrent",),
    "total_equity": ("StockholdersEquity", "PartnersCapital", "MembersEquity"),
    "equity_incl_nci": ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",),
    "minority_interest": ("MinorityInterest",),
    "preferred_equity": ("PreferredStockValue", "PreferredStockValueOutstanding"),
    "pension_funded_status": ("DefinedBenefitPlanFundedStatusOfPlan",),
    "pension_liability": ("DefinedBenefitPensionPlanLiabilitiesNoncurrent",),
    "shares_outstanding": ("CommonStockSharesOutstanding",),
    "dei_shares_outstanding": ("EntityCommonStockSharesOutstanding",),
    # cash flow
    "depreciation_amortization": (
        "DepreciationDepletionAndAmortization",
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ),
    "stock_based_comp": ("ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"),
    "capex": (
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
        "PaymentsForCapitalImprovements",
        "PaymentsToAcquireOilAndGasPropertyAndEquipment",
        "PaymentsToAcquireOilAndGasProperty",
        "PaymentsToAcquireRealEstate",
    ),
    "change_in_operating_capital": ("IncreaseDecreaseInOperatingCapital",),
    # banks
    "deposits": ("Deposits",),
    "net_interest_income": ("InterestIncomeExpenseNet",),
    "interest_income": ("InterestAndDividendIncomeOperating",),
    "provision_credit_losses": (
        "ProvisionForLoanLeaseAndOtherLosses",
        "ProvisionForLoanAndLeaseLosses",
        "ProvisionForLoanLossesExpensed",
    ),
    "goodwill": ("Goodwill",),
    "intangibles": ("IntangibleAssetsNetExcludingGoodwill", "FiniteLivedIntangibleAssetsNet"),
    "tier1_ratio": (
        "TierOneRiskBasedCapitalToRiskWeightedAssets",
        "CommonEquityTierOneCapitalToRiskWeightedAssets",
    ),
    # insurers
    "premiums_earned": ("PremiumsEarnedNet", "PremiumsEarnedNetPropertyAndCasualty"),
    "losses_incurred": (
        "PolicyholderBenefitsAndClaimsIncurredNet",
        "IncurredClaimsPropertyCasualtyAndLiability",
    ),
    "dac_amortization": ("DeferredPolicyAcquisitionCostAmortizationExpense",),
    "underwriting_other": ("OtherUnderwritingExpense", "GeneralAndAdministrativeExpense"),
    # REITs
    "re_gross": ("RealEstateInvestmentPropertyAtCost",),
    "re_net": ("RealEstateInvestmentPropertyNet",),
    "re_accumulated_depreciation": ("RealEstateInvestmentPropertyAccumulatedDepreciation",),
    "re_gains_on_sale": (
        "GainsLossesOnSalesOfInvestmentRealEstate",
        "GainLossOnSaleOfPropertiesNetOfApplicableIncomeTaxes",
    ),
    "re_impairments": ("ImpairmentOfRealEstate",),
    # E&P
    "standardized_measure": (
        "StandardizedMeasureOfDiscountedFutureNetCashFlowsRelatingToProvedOilAndGasReserves",
    ),
    "proved_reserves": ("ProvedDevelopedAndUndevelopedReservesNet",),
}

# NWC components (cash-flow statement). XBRL sign: IncreaseDecreaseIn<Asset> > 0 means the asset grew
# (cash use); IncreaseDecreaseIn<Liability> > 0 means the liability grew (cash source).
NWC_ASSET_TAGS: tuple[str, ...] = (
    "IncreaseDecreaseInAccountsReceivable",
    "IncreaseDecreaseInOtherReceivables",
    "IncreaseDecreaseInInventories",
    "IncreaseDecreaseInPrepaidDeferredExpenseAndOtherAssets",
    "IncreaseDecreaseInOtherOperatingAssets",
    "IncreaseDecreaseInOtherCurrentAssets",
)
NWC_LIABILITY_TAGS: tuple[str, ...] = (
    "IncreaseDecreaseInAccountsPayable",
    "IncreaseDecreaseInAccruedLiabilities",
    "IncreaseDecreaseInContractWithCustomerLiability",
    "IncreaseDecreaseInDeferredRevenue",
    "IncreaseDecreaseInOtherOperatingLiabilities",
    "IncreaseDecreaseInOtherCurrentLiabilities",
)
NWC_AP_AND_ACCRUED = "IncreaseDecreaseInAccountsPayableAndAccruedLiabilities"

# Tags that are only an approximation of the concept they are mapped to -> always flagged when used.
PROXY_TAGS: dict[str, str] = {
    "PaymentsToAcquireRealEstate": "real-estate acquisitions used as capex proxy",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": "includes restricted cash",
    "Depreciation": "depreciation only (amortization not reported)",
    "WeightedAverageNumberOfSharesOutstandingBasic": "basic (not diluted) weighted shares",
    "WeightedAverageLimitedPartnershipUnitsOutstanding": "basic (not diluted) LP units",
    "RevenuesNetOfInterestExpense": "revenue net of interest expense (financial company)",
}

OIL_UNITS: dict[str, float] = {"MMBbls": 1.0, "MMBbl": 1.0, "MBbls": 1e-3, "MBbl": 1e-3, "bbl": 1e-6}
GAS_UNITS: dict[str, float] = {"Bcf": 1.0, "MMcf": 1e-3, "Mcf": 1e-6, "Tcf": 1e3}


# Cover-page share counts that multi-class issuers report once per class (dimension dropped in companyfacts).
PER_CLASS_TAGS = frozenset({"EntityCommonStockSharesOutstanding"})


class NormalizationError(ValueError):
    """companyfacts has no usable annual (10-K) data."""


# ---------------------------------------------------------------------------------------------------
# Small helpers (also used by the classifier / segments / client)
# ---------------------------------------------------------------------------------------------------


def _d(s: str) -> date:
    return date.fromisoformat(s)


def fiscal_year_from_end(end: date) -> int:
    """Fallback fiscal-year label from a period end: calendar year of the end date, except 52/53-week
    years ending in the first week of January belong to the previous year."""
    if end.month == 1 and end.day <= 7:
        return end.year - 1
    return end.year


def present_tags(companyfacts: Mapping[str, Any]) -> set[str]:
    """All tag names (without taxonomy prefix) that have at least one fact."""
    out: set[str] = set()
    for tags in (companyfacts.get("facts") or {}).values():
        out.update(tags.keys())
    return out


def _recent(submissions: Mapping[str, Any]) -> list[dict[str, Any]]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    forms = recent.get("form") or []
    rows = []
    for i in range(len(forms)):
        rows.append({k: (v[i] if i < len(v) else None) for k, v in recent.items() if isinstance(v, list)})
    rows.sort(key=lambda r: (r.get("filingDate") or "", r.get("accessionNumber") or ""), reverse=True)
    return rows


def latest_periodic_accession(submissions: Mapping[str, Any]) -> str | None:
    """Accession number of the most recent 10-K/10-Q (incl. amendments/transition reports)."""
    for row in _recent(submissions):
        if row.get("form") in PERIODIC_FORMS:
            return row.get("accessionNumber")
    return None


def latest_10k(submissions: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """(accession, primary_document, report_date) of the most recent 10-K (for segment extraction)."""
    for row in _recent(submissions):
        if row.get("form") in ANNUAL_FORMS:
            return row["accessionNumber"], row.get("primaryDocument") or "", row.get("reportDate") or ""
    return None


def filer_forms(submissions: Mapping[str, Any]) -> set[str]:
    recent = (submissions.get("filings") or {}).get("recent") or {}
    return set(recent.get("form") or [])


def entity_type(submissions: Mapping[str, Any]) -> str:
    return str(submissions.get("entityType") or "")


def sic(submissions: Mapping[str, Any]) -> str:
    return str(submissions.get("sic") or "")


def sic_description(submissions: Mapping[str, Any]) -> str:
    return str(submissions.get("sicDescription") or "")


def fiscal_year_end_month(submissions: Mapping[str, Any]) -> int:
    """Month of the fiscal year end from submissions `fiscalYearEnd` ("MMDD"). A 52/53-week year that
    ends in the first days of January (e.g. "0102") is treated as a December year end. Default 12."""
    raw = str(submissions.get("fiscalYearEnd") or "").strip()
    if len(raw) != 4 or not raw.isdigit():
        return 12
    month, day = int(raw[:2]), int(raw[2:])
    if month == 1 and day <= 7:
        return 12
    return month if 1 <= month <= 12 else 12


def latest_value(companyfacts: Mapping[str, Any], tag: str, unit: str = USD) -> float | None:
    """Latest reported value (by period end, then filed) for a tag across periodic forms — for quick
    materiality checks in the classifier."""
    facts = FactIndex(companyfacts).facts(tag, unit)
    if not facts:
        return None
    best = max(facts, key=lambda f: (f.end, f.filed))
    return best.val


def latest_annual_values(
    companyfacts: Mapping[str, Any], tags: Iterable[str] | None = None
) -> dict[str, float]:
    """{tag: value at the latest fiscal-year end} for USD tags (instants at, or annual durations ending
    on, the FY end). Feeds the classifier's `tag_latest_values` materiality checks."""
    idx = FactIndex(companyfacts)
    periods = _annual_periods(idx)
    if not periods:
        return {}
    end = periods[-1].end
    out: dict[str, float] = {}
    for tag in tags if tags is not None else sorted(idx._raw):
        f = idx.annual(tag, USD).get(end) or idx.instants(tag, USD).get(end)
        if f is not None:
            out[tag] = f.val
    return out


OCF_TAGS: tuple[str, ...] = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)


def operating_cash_flow_history(companyfacts: Mapping[str, Any]) -> tuple[float, ...]:
    """Annual operating cash flow, oldest -> newest (only years where it is reported)."""
    idx = FactIndex(companyfacts)
    out = []
    for p in _annual_periods(idx):
        for tag in OCF_TAGS:
            f = idx.annual(tag, USD).get(p.end)
            if f is not None:
                out.append(f.val)
                break
    return tuple(out)


# ---------------------------------------------------------------------------------------------------
# Fact index
# ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fact:
    tag: str
    unit: str
    start: date | None
    end: date
    val: float
    accn: str
    fy: int | None
    fp: str | None
    form: str
    filed: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days if self.start else 0


class FactIndex:
    """Parsed, restatement-resolved view of a companyfacts document."""

    def __init__(self, companyfacts: Mapping[str, Any]):
        self._raw: dict[str, dict[str, list[dict[str, Any]]]] = {}
        taxonomies = companyfacts.get("facts") or {}
        # us-gaap wins on name clashes; other taxonomies (dei, srt, ifrs-full, custom) fill in.
        order = sorted(taxonomies.keys(), key=lambda t: (t != "us-gaap", t != "dei", t))
        for taxonomy in order:
            for tag, body in (taxonomies.get(taxonomy) or {}).items():
                if tag in self._raw:
                    continue
                self._raw[tag] = (body or {}).get("units") or {}
        self._cache: dict[tuple[str, str, str], Any] = {}
        # (tag, unit, end) -> number of per-class facts summed into that instant (see instants())
        self.share_classes: dict[tuple[str, str, date], int] = {}

    def has(self, tag: str) -> bool:
        return tag in self._raw

    def units(self, tag: str) -> list[str]:
        return list(self._raw.get(tag, {}).keys())

    def facts(self, tag: str, unit: str) -> list[Fact]:
        key = ("facts", tag, unit)
        if key not in self._cache:
            out: list[Fact] = []
            for f in self._raw.get(tag, {}).get(unit, []):
                form = f.get("form") or ""
                if form not in PERIODIC_FORMS or f.get("val") is None or not f.get("end"):
                    continue
                out.append(
                    Fact(
                        tag=tag,
                        unit=unit,
                        start=_d(f["start"]) if f.get("start") else None,
                        end=_d(f["end"]),
                        val=float(f["val"]),
                        accn=f.get("accn") or "",
                        fy=int(f["fy"]) if f.get("fy") else None,
                        fp=f.get("fp"),
                        form=form,
                        filed=_d(f["filed"]) if f.get("filed") else date.min,
                    )
                )
            self._cache[key] = out
        return self._cache[key]

    def durations(self, tag: str, unit: str) -> dict[tuple[date, date], Fact]:
        """(start, end) -> fact; restatements resolved by latest `filed` (then accession)."""
        key = ("dur", tag, unit)
        if key not in self._cache:
            out: dict[tuple[date, date], Fact] = {}
            for f in self.facts(tag, unit):
                if f.start is None:
                    continue
                k = (f.start, f.end)
                cur = out.get(k)
                if cur is None or (f.filed, f.accn) > (cur.filed, cur.accn):
                    out[k] = f
            self._cache[key] = out
        return self._cache[key]

    def annual(self, tag: str, unit: str) -> dict[date, Fact]:
        """end -> latest-filed fiscal-year duration fact."""
        key = ("ann", tag, unit)
        if key not in self._cache:
            out: dict[date, Fact] = {}
            for (_s, e), f in self.durations(tag, unit).items():
                if not ANNUAL_MIN_DAYS <= f.days <= ANNUAL_MAX_DAYS:
                    continue
                cur = out.get(e)
                if cur is None or (f.filed, f.accn) > (cur.filed, cur.accn):
                    out[e] = f
            self._cache[key] = out
        return self._cache[key]

    def instants(self, tag: str, unit: str) -> dict[date, Fact]:
        key = ("inst", tag, unit)
        if key not in self._cache:
            facts = [f for f in self.facts(tag, unit) if f.start is None]
            if tag in PER_CLASS_TAGS:
                facts = self._sum_share_classes(tag, unit, facts)
            out: dict[date, Fact] = {}
            for f in facts:
                cur = out.get(f.end)
                if cur is None or (f.filed, f.accn) > (cur.filed, cur.accn):
                    out[f.end] = f
            self._cache[key] = out
        return self._cache[key]

    def _sum_share_classes(self, tag: str, unit: str, facts: list[Fact]) -> list[Fact]:
        """companyfacts drops XBRL dimensions, so a multi-class issuer's cover page (e.g. Alphabet's
        Class A / B / C) shows up as several facts with the same end date and accession. The company
        total is their sum; picking any single one understates the share count."""
        groups: dict[tuple[date, str], list[Fact]] = {}
        for f in facts:
            groups.setdefault((f.end, f.accn), []).append(f)
        out: list[Fact] = []
        for (end, _accn), group in groups.items():
            if len(group) == 1:
                out.append(group[0])
                continue
            self.share_classes[(tag, unit, end)] = max(
                len(group), self.share_classes.get((tag, unit, end), 0)
            )
            first = group[0]
            out.append(
                Fact(
                    tag=first.tag,
                    unit=first.unit,
                    start=None,
                    end=end,
                    val=sum(f.val for f in group),
                    accn=first.accn,
                    fy=first.fy,
                    fp=first.fp,
                    form=first.form,
                    filed=max(f.filed for f in group),
                )
            )
        return out


# ---------------------------------------------------------------------------------------------------
# Period views
# ---------------------------------------------------------------------------------------------------


class _Val(NamedTuple):
    value: float
    tag: str
    note: str | None = None


def _near(a: date, b: date, tol: int) -> bool:
    return abs((a - b).days) <= tol


@dataclass
class _Ytd:
    start: date
    end: date
    fy: int | None


class _View:
    """Resolves tag lists to values for one period (an annual period or the TTM period)."""

    def __init__(
        self,
        idx: FactIndex,
        label: str,
        period: FiscalPeriod,
        annual_end: date,
        annual_start: date,
        ytd: _Ytd | None = None,
    ):
        self.idx = idx
        self.label = label
        self.period = period
        self.end = annual_end
        self.start = annual_start
        self.ytd = ytd  # only for the TTM view

    @property
    def is_ttm(self) -> bool:
        return self.period.is_ttm

    # -- raw lookups -------------------------------------------------------------------------------
    def _annual(self, tags: Sequence[str], unit: str) -> _Val | None:
        for tag in tags:
            f = self.idx.annual(tag, unit).get(self.end)
            if f is not None:
                return _Val(f.val, tag)
        return None

    def _ytd_pair(self, tag: str, unit: str) -> tuple[float, float] | None:
        assert self.ytd is not None
        ytd = prior = None
        span = (self.ytd.end - self.ytd.start).days
        prior_end = self.ytd.end - timedelta(days=365)
        for (s, e), f in self.idx.durations(tag, unit).items():
            if e == self.ytd.end and _near(s, self.ytd.start, 3):
                ytd = f.val
            elif _near(s, self.start, 7) and _near(e, prior_end, 10) and abs(f.days - span) <= 10:
                prior = f.val
        if ytd is None or prior is None:
            return None
        return ytd, prior

    # -- public ------------------------------------------------------------------------------------
    def flow(self, tags: Sequence[str], unit: str = USD) -> _Val | None:
        annual = self._annual(tags, unit)
        if not self.is_ttm or self.ytd is None:
            return annual
        if annual is None:
            return None
        for tag in tags:
            pair = self._ytd_pair(tag, unit)
            if pair is not None:
                return _Val(annual.value + pair[0] - pair[1], annual.tag)
        return _Val(annual.value, annual.tag, "TTM uses latest fiscal-year value (no YTD data)")

    def shares(self, tags: Sequence[str]) -> _Val | None:
        annual = self._annual(tags, SHARES)
        if not self.is_ttm or self.ytd is None:
            return annual
        for tag in tags:
            quarter = ytd = None
            for (s, e), f in self.idx.durations(tag, SHARES).items():
                if e != self.ytd.end:
                    continue
                if 80 <= f.days <= 100:
                    quarter = f.val
                if _near(s, self.ytd.start, 3):
                    ytd = f.val
            if quarter is not None or ytd is not None:
                return _Val(quarter if quarter is not None else ytd, tag)  # type: ignore[arg-type]
        if annual is not None:
            return _Val(annual.value, annual.tag, "TTM uses latest fiscal-year value (no YTD data)")
        return None

    def instant(self, tags: Sequence[str], unit: str = USD) -> _Val | None:
        when = self.ytd.end if (self.is_ttm and self.ytd is not None) else self.end
        for tag in tags:
            f = self.idx.instants(tag, unit).get(when)
            if f is not None:
                return _Val(f.val, tag)
        if when != self.end:
            for tag in tags:
                f = self.idx.instants(tag, unit).get(self.end)
                if f is not None:
                    return _Val(f.val, tag, "TTM balance uses latest fiscal-year-end value")
        return None

    def instant_near(self, tags: Sequence[str], unit: str, max_days: int = 120) -> _Val | None:
        """Closest instant on/after the period end (e.g. dei cover-page share counts)."""
        when = self.ytd.end if (self.is_ttm and self.ytd is not None) else self.end
        best: tuple[int, _Val] | None = None
        for tag in tags:
            for end, f in self.idx.instants(tag, unit).items():
                delta = (end - when).days
                if 0 <= delta <= max_days and (best is None or delta < best[0]):
                    n = self.idx.share_classes.get((tag, unit, end), 1)
                    note = f"summed across {n} share classes reported separately" if n > 1 else None
                    best = (delta, _Val(f.val, tag, note))
        return best[1] if best else None


# ---------------------------------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------------------------------


class _Flags:
    def __init__(self) -> None:
        self._groups: dict[str, list[str]] = {}

    def add(self, msg: str, period: str | None = None) -> None:
        periods = self._groups.setdefault(msg, [])
        if period and period not in periods:
            periods.append(period)

    def render(self) -> list[str]:
        return [f"{m} ({', '.join(p)})" if p else m for m, p in self._groups.items()]


class _Resolver:
    """Concept resolution for one view, recording flags."""

    def __init__(self, view: _View, flags: _Flags):
        self.v = view
        self.flags = flags

    def _note(self, concept: str, val: _Val | None) -> _Val | None:
        if val is None:
            return None
        if val.tag in PROXY_TAGS:
            self.flags.add(f"{concept}: proxy tag {val.tag} ({PROXY_TAGS[val.tag]})", self.v.label)
        if val.note:
            self.flags.add(f"{concept}: {val.note}", self.v.label)
        return val

    def flow(self, concept: str, unit: str = USD) -> float | None:
        val = self._note(concept, self.v.flow(TAG_MAP[concept], unit))
        return val.value if val else None

    def instant(self, concept: str, unit: str = USD) -> float | None:
        val = self._note(concept, self.v.instant(TAG_MAP[concept], unit))
        return val.value if val else None

    def shares(self, concept: str) -> float | None:
        val = self._note(concept, self.v.shares(TAG_MAP[concept]))
        return val.value if val else None

    def default(self, concept: str, value: float | None, fallback: float = 0.0) -> float:
        if value is None:
            self.flags.add(f"{concept}: not reported, assumed {fallback:g}", self.v.label)
            return fallback
        return value

    def derived(self, concept: str, how: str) -> None:
        self.flags.add(f"{concept}: derived as {how}", self.v.label)

    def sum_flows(self, tags: Iterable[str]) -> tuple[float, list[str]] | None:
        total, used = 0.0, []
        for tag in tags:
            val = self.v.flow((tag,))
            if val is not None:
                total += val.value
                used.append(tag)
                if val.note:
                    self.flags.add(f"{tag}: {val.note}", self.v.label)
        return (total, used) if used else None


# ---------------------------------------------------------------------------------------------------
# Line builders
# ---------------------------------------------------------------------------------------------------


def _revenue(r: _Resolver) -> float:
    val = r.v.flow(TAG_MAP["revenue"])
    total = r.v.flow(("Revenues",))
    # Financial companies often tag a small ASC 606 fee-revenue subset with RevenueFromContract...;
    # when total `Revenues` is materially larger, it is the right top line.
    if val is not None and total is not None and val.tag != "Revenues" and total.value > 1.25 * val.value:
        r.flags.add(
            f"revenue: using Revenues instead of {val.tag} (the latter is a subset of total revenue)",
            r.v.label,
        )
        val = total
    val = r._note("revenue", val)
    return r.default("revenue", val.value if val else None)


def _income_statement(r: _Resolver, financial: bool) -> IncomeStatementLine:
    revenue = _revenue(r)

    cogs = r.flow("cogs")
    gross_profit = r.flow("gross_profit")
    if gross_profit is None and cogs is not None:
        gross_profit = revenue - cogs
        r.derived("gross_profit", "revenue - cogs")
    elif cogs is None and gross_profit is not None:
        cogs = revenue - gross_profit
        r.derived("cogs", "revenue - gross_profit")

    sga = r.flow("sga")
    if sga is None:
        sm, ga = r.flow("selling_marketing"), r.flow("general_admin")
        if sm is not None or ga is not None:
            sga = (sm or 0.0) + (ga or 0.0)
            r.derived("sga", "selling & marketing + general & administrative")

    rd = r.flow("rd")
    net_income_raw = r.flow("net_income")
    tax_raw = r.flow("tax_expense")
    pretax_raw = r.flow("pretax_income")
    net_income = r.default("net_income", net_income_raw)

    if pretax_raw is not None:
        pretax = pretax_raw
    elif tax_raw is not None:
        pretax = net_income + tax_raw
        r.derived("pretax_income", "net income + income tax expense")
    else:
        pretax = net_income
        r.flags.add("pretax_income: not reported, assumed equal to net income", r.v.label)
    if tax_raw is None and pretax_raw is not None:
        tax = pretax_raw - net_income
        r.derived("tax_expense", "pretax income - net income")
    else:
        tax = r.default("tax_expense", tax_raw)

    interest = r.default("interest_expense", r.flow("interest_expense"))

    op = r.flow("operating_income")
    if op is None:
        if financial:
            op = pretax
            r.derived("operating_income", "pretax income (financial company, no operating income line)")
        else:
            op = pretax + interest
            r.derived("operating_income", "pretax income + interest expense")

    shares = r.shares("diluted_shares")
    if shares is None:
        dei = r._note(
            "diluted_shares", r.v.instant_near(TAG_MAP["dei_shares_outstanding"], SHARES, max_days=150)
        )
        if dei is not None:
            shares = dei.value
            r.derived("diluted_shares", "cover-page shares outstanding (dei)")
    shares = r.default("diluted_shares", shares)

    return IncomeStatementLine(
        period=r.v.period,
        revenue=revenue,
        cogs=cogs,
        gross_profit=gross_profit,
        sga=sga,
        rd=rd,
        operating_income=op,
        interest_expense=interest,
        pretax_income=pretax,
        tax_expense=tax,
        net_income=net_income,
        diluted_shares=shares,
    )


def _total_debt(r: _Resolver) -> float:
    parts: dict[str, float] = {}

    def take(concept: str) -> float | None:
        val = r.v.instant(TAG_MAP[concept])
        if val is None:
            return None
        if val.note:
            r.flags.add(f"total_debt: {val.note}", r.v.label)
        parts[val.tag] = val.value
        return val.value

    noncurrent = take("ltd_noncurrent")
    if noncurrent is None and r.v.instant(TAG_MAP["ltd_total"]) is not None:
        take("ltd_total")  # includes the current portion
        take("commercial_paper")
        take("short_term_borrowings")
    elif take("debt_current") is None:
        take("ltd_current")
        take("commercial_paper")
        take("short_term_borrowings")

    if take("finance_lease_total") is None:
        take("finance_lease_current")
        take("finance_lease_noncurrent")

    if not parts:
        r.flags.add("total_debt: no debt tags reported, assumed 0", r.v.label)
        return 0.0
    r.derived("total_debt", "sum of " + " + ".join(parts))
    return sum(parts.values())


def _balance_sheet(r: _Resolver) -> BalanceSheetLine:
    cash = r.default("cash_and_equivalents", r.instant("cash"))
    sti = r.default("short_term_investments", r.instant("short_term_investments"))
    total_debt = _total_debt(r)

    oll = r.instant("operating_lease_total")
    if oll is None:
        cur, nonc = r.instant("operating_lease_current"), r.instant("operating_lease_noncurrent")
        if cur is not None or nonc is not None:
            oll = (cur or 0.0) + (nonc or 0.0)
            r.derived("operating_lease_liability", "current + noncurrent operating lease liability")
    oll = r.default("operating_lease_liability", oll)

    mi_raw = r.instant("minority_interest")
    equity = r.instant("total_equity")
    if equity is None:
        incl = r.instant("equity_incl_nci")
        if incl is not None:
            equity = incl - (mi_raw or 0.0)
            r.derived("total_equity", "equity incl. NCI - minority interest")
    equity = r.default("total_equity", equity)
    mi = r.default("minority_interest", mi_raw)
    pref = r.default("preferred_equity", r.instant("preferred_equity"))

    pension: float | None = None
    funded = r.instant("pension_funded_status")
    if funded is not None:
        pension = max(0.0, -funded)
    else:
        liab = r.instant("pension_liability")
        if liab is not None:
            pension = liab
            r.derived("pension_deficit", "noncurrent pension liability (funded status not reported)")

    return BalanceSheetLine(
        period=r.v.period,
        cash_and_equivalents=cash,
        short_term_investments=sti,
        total_debt=total_debt,
        operating_lease_liability=oll,
        total_equity=equity,
        minority_interest=mi,
        preferred_equity=pref,
        pension_deficit=pension,
    )


def _change_in_nwc(r: _Resolver) -> float:
    direct = r.flow("change_in_operating_capital")
    if direct is not None:
        return direct
    assets = r.sum_flows(NWC_ASSET_TAGS)
    liab_tags = list(NWC_LIABILITY_TAGS)
    if (
        r.v.flow(("IncreaseDecreaseInAccountsPayable",)) is None
        and r.v.flow(("IncreaseDecreaseInAccruedLiabilities",)) is None
    ):
        liab_tags.append(NWC_AP_AND_ACCRUED)
    liabs = r.sum_flows(liab_tags)
    if assets is None and liabs is None:
        r.flags.add("change_in_nwc: no working-capital tags reported, assumed 0", r.v.label)
        return 0.0
    r.derived("change_in_nwc", "sum of operating asset increases - operating liability increases")
    return (assets[0] if assets else 0.0) - (liabs[0] if liabs else 0.0)


def _cash_flow(r: _Resolver) -> CashFlowLine:
    return CashFlowLine(
        period=r.v.period,
        depreciation_amortization=r.default("depreciation_amortization", r.flow("depreciation_amortization")),
        stock_based_comp=r.default("stock_based_comp", r.flow("stock_based_comp")),
        capex=r.default("capex", r.flow("capex")),
        change_in_nwc=_change_in_nwc(r),
    )


def _bank(r: _Resolver, bs: BalanceSheetLine) -> BankSpecificLine | None:
    deposits = r.instant("deposits")
    if deposits is None:
        return None
    nii = r.flow("net_interest_income")
    if nii is None:
        ii, ie = r.flow("interest_income"), r.v.flow(("InterestExpense",))
        if ii is not None:
            nii = ii - (ie.value if ie else 0.0)
            r.derived("net_interest_income", "interest income - interest expense")
    nii = r.default("net_interest_income", nii)
    provision = r.default("provision_for_credit_losses", r.flow("provision_credit_losses"))
    goodwill = r.instant("goodwill") or 0.0
    intangibles = r.instant("intangibles") or 0.0
    tbv = bs.total_equity - bs.preferred_equity - goodwill - intangibles
    r.derived("tangible_book_value", "equity - preferred - goodwill - intangibles")
    tier1 = r.instant("tier1_ratio", PURE)
    return BankSpecificLine(
        period=r.v.period,
        net_interest_income=nii,
        provision_for_credit_losses=provision,
        total_deposits=deposits,
        tangible_book_value=tbv,
        tier1_capital_ratio=tier1,
    )


def _insurer(
    r: _Resolver, bs: BalanceSheetLine, is_line: IncomeStatementLine, pc: bool
) -> InsurerSpecificLine | None:
    premiums = r.flow("premiums_earned")
    if premiums is None:
        return None
    losses = r.flow("losses_incurred")
    loss_ratio = combined = None
    if losses is not None and premiums:
        loss_ratio = losses / premiums
        r.derived("loss_and_lae_ratio", "claims incurred / net premiums earned")
        if pc:
            dac = r.flow("dac_amortization") or 0.0
            other = r.flow("underwriting_other") or 0.0
            combined = (losses + dac + other) / premiums
            r.derived("combined_ratio", "(claims + DAC amortization + underwriting/G&A expense) / premiums")
    shares = r.instant("shares_outstanding", SHARES)
    if shares is None:
        dei = r.v.instant_near(TAG_MAP["dei_shares_outstanding"], SHARES)
        shares = dei.value if dei else is_line.diluted_shares
    bvps = bs.total_equity / shares if shares else 0.0
    r.derived("book_value_per_share", "stockholders' equity / shares outstanding")
    return InsurerSpecificLine(
        period=r.v.period,
        net_premiums_earned=premiums,
        loss_and_lae_ratio=loss_ratio,
        combined_ratio=combined,
        book_value_per_share=bvps,
    )


def _reit(r: _Resolver, is_line: IncomeStatementLine, cf: CashFlowLine) -> ReitSpecificLine | None:
    gross = r.instant("re_gross")
    accum = r.instant("re_accumulated_depreciation")
    if gross is None:
        net = r.instant("re_net")
        if net is None:
            return None
        gross = net + (accum or 0.0)
        r.derived("real_estate_investments_gross", "net real estate + accumulated depreciation")
    accum = r.default("accumulated_depreciation", accum)
    gains = r.flow("re_gains_on_sale") or 0.0
    impairments = r.flow("re_impairments") or 0.0
    ffo = is_line.net_income + cf.depreciation_amortization - gains + impairments
    r.derived("ffo", "net income + D&A - gains on sale + real-estate impairments (NAREIT approximation)")
    return ReitSpecificLine(
        period=r.v.period,
        real_estate_investments_gross=gross,
        accumulated_depreciation=accum,
        ffo=ffo,
        affo=None,
    )


def _reserves(idx: FactIndex, end: date, units: Mapping[str, float]) -> float | None:
    for tag in TAG_MAP["proved_reserves"]:
        for unit, scale in units.items():
            f = idx.instants(tag, unit).get(end)
            if f is not None:
                return f.val * scale
    return None


def _ep(r: _Resolver) -> EpSpecificLine | None:
    smog = r.instant("standardized_measure")
    if smog is None:
        return None
    return EpSpecificLine(
        period=r.v.period,
        standardized_measure_disc_future_cash_flows=smog,
        proved_reserves_oil_mmbbl=_reserves(r.v.idx, r.v.end, OIL_UNITS),
        proved_reserves_gas_bcf=_reserves(r.v.idx, r.v.end, GAS_UNITS),
    )


# ---------------------------------------------------------------------------------------------------
# Period discovery
# ---------------------------------------------------------------------------------------------------

_ANCHORS: tuple[str, ...] = TAG_MAP["revenue"] + TAG_MAP["net_income"] + TAG_MAP["operating_income"]


@dataclass
class _AnnualPeriod:
    start: date
    end: date
    fiscal_year: int


def _annual_periods(idx: FactIndex) -> list[_AnnualPeriod]:
    starts: dict[date, date] = {}
    fy_by_end: dict[date, int] = {}
    # fiscal year label: the `fy` of the 10-K whose primary (latest) period is this end
    max_end_by_accn: dict[str, date] = {}
    for tag in _ANCHORS:
        for f in idx.annual(tag, USD).values():
            if f.form not in ANNUAL_FORMS and f.fp != "FY":
                continue
            starts.setdefault(f.end, f.start)  # type: ignore[arg-type]
        for f in idx.facts(tag, USD):
            if f.form in ANNUAL_FORMS and f.start and ANNUAL_MIN_DAYS <= f.days <= ANNUAL_MAX_DAYS:
                if f.accn not in max_end_by_accn or f.end > max_end_by_accn[f.accn]:
                    max_end_by_accn[f.accn] = f.end
    for tag in _ANCHORS:
        for f in idx.facts(tag, USD):
            if f.fy and max_end_by_accn.get(f.accn) == f.end and f.form in ANNUAL_FORMS:
                fy_by_end.setdefault(f.end, f.fy)
    return [
        _AnnualPeriod(start=starts[e], end=e, fiscal_year=fy_by_end.get(e, fiscal_year_from_end(e)))
        for e in sorted(starts)
    ]


def _find_ytd(idx: FactIndex, latest: _AnnualPeriod) -> _Ytd | None:
    fy_start = latest.end + timedelta(days=1)
    best: Fact | None = None
    for tag in _ANCHORS:
        for (s, e), f in idx.durations(tag, USD).items():
            if e <= latest.end or f.days >= ANNUAL_MIN_DAYS or not _near(s, fy_start, 7):
                continue
            if f.form not in QUARTERLY_FORMS:
                continue
            if best is None or e > best.end:
                best = f
    if best is None or best.start is None:
        return None
    return _Ytd(start=best.start, end=best.end, fy=best.fy)


def _share_split_flags(rows: list[IncomeStatementLine], flags: _Flags) -> None:
    for prev, cur in zip(rows, rows[1:], strict=False):
        if not prev.diluted_shares or not cur.diluted_shares:
            continue
        ratio = cur.diluted_shares / prev.diluted_shares
        for n in (2, 3, 4, 5, 7, 8, 10, 20):
            if abs(ratio - n) <= 0.1 * n or abs(1 / ratio - n) <= 0.1 * n:
                kind = f"{n}-for-1 split" if ratio > 1 else f"1-for-{n} reverse split"
                flags.add(
                    f"diluted_shares: possible {kind} between FY{prev.period.fiscal_year} and "
                    f"{'TTM' if cur.period.is_ttm else f'FY{cur.period.fiscal_year}'}; "
                    "earlier share counts are as originally reported (not split-adjusted)"
                )
                break


def _revenue_jump_flags(rows: list[IncomeStatementLine], flags: _Flags) -> None:
    annual = [r for r in rows if not r.period.is_ttm]
    for prev, cur in zip(annual, annual[1:], strict=False):
        if prev.revenue > 0 and cur.revenue > 0:
            change = cur.revenue / prev.revenue - 1
            if abs(change) > REVENUE_JUMP_THRESHOLD:
                flags.add(
                    f"revenue {'jump' if change > 0 else 'drop'} FY{cur.period.fiscal_year} "
                    f"({change:+.0%} vs FY{prev.period.fiscal_year}; M&A, divestiture or restatement?)"
                )


# ---------------------------------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------------------------------


def normalize(
    companyfacts: Mapping[str, Any], submissions: Mapping[str, Any], ticker: str
) -> NormalizedFinancials:
    idx = FactIndex(companyfacts)
    periods = _annual_periods(idx)
    if not periods:
        raise NormalizationError(f"{ticker}: no annual 10-K facts in companyfacts")

    flags = _Flags()
    sic_code = sic(submissions)
    is_bank = idx.has("Deposits") and (
        idx.has("InterestIncomeExpenseNet") or idx.has("InterestAndDividendIncomeOperating")
    )
    is_insurer = any(idx.has(t) for t in TAG_MAP["premiums_earned"])
    financial = is_bank or is_insurer
    pc_insurer = sic_code.startswith("633")

    views: list[_View] = [
        _View(
            idx,
            f"FY{p.fiscal_year}",
            FiscalPeriod(fiscal_year=p.fiscal_year, period_end=p.end.isoformat()),
            p.end,
            p.start,
        )
        for p in periods
    ]
    latest = periods[-1]
    ytd = _find_ytd(idx, latest)
    if ytd is None:
        flags.add(f"TTM: no 10-Q after FY{latest.fiscal_year}; TTM row equals the latest fiscal year")
        ttm_period = FiscalPeriod(
            fiscal_year=latest.fiscal_year, period_end=latest.end.isoformat(), is_ttm=True
        )
    else:
        ttm_period = FiscalPeriod(
            fiscal_year=ytd.fy or latest.fiscal_year + 1, period_end=ytd.end.isoformat(), is_ttm=True
        )
    views.append(_View(idx, "TTM", ttm_period, latest.end, latest.start, ytd))

    income: list[IncomeStatementLine] = []
    balance: list[BalanceSheetLine] = []
    cash: list[CashFlowLine] = []
    bank: list[BankSpecificLine] = []
    insurer: list[InsurerSpecificLine] = []
    reit: list[ReitSpecificLine] = []
    ep: list[EpSpecificLine] = []
    for view in views:
        r = _Resolver(view, flags)
        is_line = _income_statement(r, financial)
        bs = _balance_sheet(r)
        cf = _cash_flow(r)
        income.append(is_line)
        balance.append(bs)
        cash.append(cf)
        if (b := _bank(r, bs)) is not None:
            bank.append(b)
        if (ins := _insurer(r, bs, is_line, pc_insurer)) is not None:
            insurer.append(ins)
        if (re := _reit(r, is_line, cf)) is not None:
            reit.append(re)
        if not view.is_ttm and (e := _ep(r)) is not None:
            ep.append(e)

    n_years = len(periods)
    if n_years < MIN_YEARS_WITHOUT_FLAG:
        flags.add(f"only {n_years} fiscal years of annual data available")
    _revenue_jump_flags(income, flags)
    _share_split_flags(income, flags)

    accession = latest_periodic_accession(submissions) or _latest_accn_in_facts(idx)
    cik_raw = companyfacts.get("cik") or submissions.get("cik") or ""
    return NormalizedFinancials(
        ticker=ticker.upper(),
        cik=str(cik_raw).zfill(10),
        fiscal_year_end_month=fiscal_year_end_month(submissions) if submissions else latest.end.month,
        income_statements=income,
        balance_sheets=balance,
        cash_flows=cash,
        bank_data=bank,
        insurer_data=insurer,
        reit_data=reit,
        ep_data=ep,
        data_confidence_flags=flags.render(),
        accession_number=accession,
    )


def _latest_accn_in_facts(idx: FactIndex) -> str:
    best: Fact | None = None
    for tag in _ANCHORS:
        for f in idx.facts(tag, USD):
            if best is None or (f.filed, f.accn) > (best.filed, best.accn):
                best = f
    return best.accn if best else ""


@cache
def all_mapped_tags() -> frozenset[str]:
    """Every tag the normalizer may read (useful for trimming fixtures / debugging)."""
    tags: set[str] = set(NWC_ASSET_TAGS) | set(NWC_LIABILITY_TAGS) | {NWC_AP_AND_ACCRUED}
    for v in TAG_MAP.values():
        tags.update(v)
    return frozenset(tags)
