"""Rules-first classifier decision tree (spec §5.5).

Evaluation order:
  1. Hard declines (first match wins): NON_10K_FILER, LIFE_INSURER, SPAC_OR_TRUST, MLP,
     MINING, BIOTECH_PRECOMMERCIAL, INSUFFICIENT_DATA.
  2. Model selection. Every candidate model gets a fit score in [0, 1]; the ordering of the
     spec's "first match wins" list is encoded in the score levels of a *matched* rule
     (bank 0.95 > P&C 0.93 > REIT 0.92 > E&P 0.90 > SOTP/FCFE/FCFF), so the top-scoring
     candidate is exactly the first matching rule. Near-ties (see ``llm_tiebreak``) are the
     genuinely ambiguous cases.

All functions here are pure: no I/O, no LLM.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from app.classify.windows import annual_income_statements, historical_window_years
from app.data.edgar.normalize import window_flags
from app.schemas.company import ClassificationResult, CompanySnapshot, DeclineReason, ModelType
from app.schemas.financials import IncomeStatementLine, NormalizedFinancials, SegmentLine

# =======================================================================================
# Input contract
# =======================================================================================


@dataclass(frozen=True)
class ClassificationSignals:
    company: CompanySnapshot
    financials: NormalizedFinancials
    filer_forms: frozenset[str]  # forms seen in submissions, e.g. {"10-K","10-Q","8-K"} or {"20-F","6-K"}
    entity_type: str  # submissions entityType, e.g. "operating", "partnership"
    present_tags: frozenset[str]  # us-gaap tags present in companyfacts
    tag_latest_values: Mapping[str, float]  # latest annual value per tag, for materiality checks
    years_public: float | None = None
    operating_cash_flow_history: tuple[float, ...] = ()  # oldest -> newest annual OCF


# =======================================================================================
# Constants (TUNABLE ones are marked)
# =======================================================================================

DOMESTIC_FORMS = frozenset({"10-K", "10-K/A", "10-KT", "10-KT/A", "10-Q", "10-Q/A"})
FOREIGN_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A", "6-K"})

LIFE_INSURER_SICS = frozenset({"6311"})
SPAC_SICS = frozenset({"6770"})  # blank checks
# Royalty / grantor trusts. NOTE: 6798 (REITs) is deliberately NOT here.
TRUST_SICS = frozenset({"6792", "6795", "6733"})
# SIC codes where publicly traded partnerships (MLPs) cluster.
MLP_SICS = frozenset(
    {"1311", "1321", "1381", "4610", "4612", "4613", "4619", "4922", "4923", "4924", "5171", "5172", "6792"}
)
PARTNERSHIP_ENTITY_TYPES = frozenset({"partnership", "limited partnership"})
MINING_SIC_RANGES: tuple[tuple[int, int], ...] = ((1000, 1099), (1200, 1299))
REIT_SICS = frozenset({"6798"})
EP_SICS = frozenset({"1311", "1381", "1382", "1389"})

_LP_NAME_RE = re.compile(r"\bL\.\s?P\.?(?=[\s,)]|$)|\bLP\b")
_SPAC_NAME_RE = re.compile(r"\bacquisition\s+corp(oration)?\b|\bblank\s+check\b", re.IGNORECASE)
_REIT_NAME_RE = re.compile(r"\bREIT\b|real estate investment trust", re.IGNORECASE)

# XBRL tags
TAG_DEPOSITS = "Deposits"
TAG_NII = "InterestIncomeExpenseNet"
TAG_PREMIUMS = "PremiumsEarnedNet"
TAG_REAL_ESTATE = "RealEstateInvestmentPropertyNet"
TAG_SMOG = "StandardizedMeasureOfDiscountedFutureNetCashFlowsRelatingToProvedOilAndGasReserves"
TAG_REVENUES = "Revenues"

# --- TUNABLE: biotech / pre-commercial signature -------------------------------------
BIOTECH_MAX_TTM_REVENUE = 50_000_000.0
BIOTECH_NEAR_ZERO_REVENUE = 1_000_000.0
BIOTECH_RD_TO_REVENUE_MIN = 2.0  # "R&D/revenue far exceeds 1"
BIOTECH_MIN_YEARS_PUBLIC = 2.0
BIOTECH_OCF_LOOKBACK_YEARS = 3  # "sustained" = every one of the last N available years negative

MIN_FISCAL_YEARS = 3  # below this -> INSUFFICIENT_DATA

# --- TUNABLE: materiality for specialized models --------------------------------------
BANK_DEPOSITS_TO_EQUITY_MIN = 3.0
BANK_NII_TO_REVENUE_MIN = 0.25
PC_PREMIUMS_TO_REVENUE_MIN = 0.30
REIT_PROPERTY_TO_CAPITAL_MIN = 0.50  # net property / (book debt + book equity)

# --- TUNABLE: SOTP ---------------------------------------------------------------------
SOTP_MIN_SEGMENT_SHARE = 0.15  # of consolidated revenue OR of positive segment op income
SOTP_STRONG_SPREAD = 0.20  # margin/growth/capex-intensity spread that earns the full SOTP score
SOTP_ASU_2023_07_FIRST_FY = 2024  # segment opex realistically disclosed from FY2024 10-Ks

# --- TUNABLE: FCFE ---------------------------------------------------------------------
FCFE_LEVERAGE_THRESHOLD = 0.50  # debt / (debt + market cap): offer FCFE as runner-up
FCFE_EXTREME_LEVERAGE = 0.70  # ... and auto-select FCFE above this, if stable
FCFE_STABILITY_MAX_RANGE = 0.10  # max - min historical book debt ratio to count as "stable"
FCFE_MIN_STABILITY_OBS = 3

# --- TUNABLE: early-stage FCFF variant -------------------------------------------------
EARLY_STAGE_MIN_GROWTH = 0.20
EARLY_STAGE_GROWTH_LOOKBACK = 3  # CAGR over up to this many years

# --- Scores ----------------------------------------------------------------------------
SCORE_BANK = 0.95
SCORE_PC_INSURER = 0.93
SCORE_REIT = 0.92
SCORE_EP = 0.90
SCORE_FCFF_BASE = 0.70
SCORE_FCFE_RUNNER_UP = 0.55
SCORE_FCFE_EXTREME = 0.82
SCORE_FCFF_FOR_FINANCIALS = 0.15  # FCFF is a poor fit once a bank/insurer rule matched
SOTP_SCORE_FLOOR = 0.50
SOTP_SCORE_RANGE = 0.40
RUNNER_UP_MIN_SCORE = 0.25
LOW_HISTORY_CONFIDENCE_PENALTY = 0.15
AMBIGUITY_WINDOW = 0.20  # confidence is reduced when top-2 gap is below this

DECLINE_CONFIDENCE: dict[DeclineReason, float] = {
    DeclineReason.NON_10K_FILER: 0.95,
    DeclineReason.LIFE_INSURER: 0.95,
    DeclineReason.SPAC_OR_TRUST: 0.90,
    DeclineReason.MLP: 0.90,
    DeclineReason.MINING: 0.90,
    DeclineReason.BIOTECH_PRECOMMERCIAL: 0.80,
    DeclineReason.INSUFFICIENT_DATA: 0.95,
}


# =======================================================================================
# Small helpers
# =======================================================================================


def _sic_int(sic_code: str) -> int | None:
    try:
        return int(str(sic_code).strip())
    except (TypeError, ValueError):
        return None


def _latest_statement(fin: NormalizedFinancials) -> IncomeStatementLine | None:
    """TTM row if present (spec: TTM is the last row), else the latest annual row."""
    return fin.income_statements[-1] if fin.income_statements else None


def _latest_balance(fin: NormalizedFinancials):
    return fin.balance_sheets[-1] if fin.balance_sheets else None


def _tag_value(signals: ClassificationSignals, tag: str) -> float | None:
    v = signals.tag_latest_values.get(tag)
    return float(v) if v is not None else None


def ttm_revenue(signals: ClassificationSignals) -> float:
    latest = _latest_statement(signals.financials)
    if latest is not None:
        return latest.revenue
    return _tag_value(signals, TAG_REVENUES) or 0.0


def is_partnership(signals: ClassificationSignals) -> bool:
    et = signals.entity_type.strip().lower()
    return (
        et in PARTNERSHIP_ENTITY_TYPES
        or "partnership" in et
        or bool(_LP_NAME_RE.search(signals.company.name))
    )


def operating_cash_flows(signals: ClassificationSignals) -> list[float]:
    """Annual OCF oldest -> newest. Falls back to a proxy (NI + D&A + SBC - ΔNWC) from the
    normalized statements when submissions-level OCF history wasn't supplied."""
    if signals.operating_cash_flow_history:
        return list(signals.operating_cash_flow_history)
    fin = signals.financials
    cf_by_year = {c.period.fiscal_year: c for c in fin.cash_flows if not c.period.is_ttm}
    out: list[float] = []
    for inc in annual_income_statements(fin):
        cf = cf_by_year.get(inc.period.fiscal_year)
        if cf is None:
            continue
        out.append(inc.net_income + cf.depreciation_amortization + cf.stock_based_comp - cf.change_in_nwc)
    return out


def revenue_cagr(
    annual: list[IncomeStatementLine], lookback: int = EARLY_STAGE_GROWTH_LOOKBACK
) -> float | None:
    if len(annual) < 2:
        return None
    n = min(lookback, len(annual) - 1)
    start, end = annual[-1 - n].revenue, annual[-1].revenue
    if start <= 0 or end <= 0:
        return None
    return (end / start) ** (1 / n) - 1


# =======================================================================================
# Step 1: hard declines
# =======================================================================================


@dataclass(frozen=True)
class _Decline:
    reason: DeclineReason
    explanation: str


def _check_declines(s: ClassificationSignals) -> _Decline | None:
    c = s.company
    sic = c.sic_code.strip()
    sic_n = _sic_int(sic)
    fin = s.financials

    # 1. Non-10-K filer (20-F / 40-F foreign private issuers etc.)
    if s.filer_forms and not (s.filer_forms & DOMESTIC_FORMS):
        foreign = sorted(s.filer_forms & FOREIGN_FORMS) or sorted(s.filer_forms)
        return _Decline(
            DeclineReason.NON_10K_FILER,
            f"files {', '.join(foreign)} rather than 10-K/10-Q; US-GAAP annual data not comparable",
        )

    # 2. Life insurer
    if sic in LIFE_INSURER_SICS:
        return _Decline(DeclineReason.LIFE_INSURER, f"SIC {sic} (life insurance) is out of scope")

    # 3. SPAC / blank check / trust
    rev = ttm_revenue(s)
    if sic in SPAC_SICS:
        return _Decline(DeclineReason.SPAC_OR_TRUST, f"SIC {sic} (blank check / SPAC)")
    if _SPAC_NAME_RE.search(c.name) and abs(rev) < BIOTECH_NEAR_ZERO_REVENUE:
        return _Decline(DeclineReason.SPAC_OR_TRUST, "blank-check name with no operating revenue")
    if sic in TRUST_SICS and not is_partnership(s):
        return _Decline(
            DeclineReason.SPAC_OR_TRUST,
            f"SIC {sic} (royalty/grantor trust) has no operating business to model",
        )

    # 4. MLP
    if sic in MLP_SICS and is_partnership(s):
        return _Decline(
            DeclineReason.MLP,
            f"publicly traded partnership (entity type '{s.entity_type}', name '{c.name}') in MLP-heavy SIC {sic}",
        )

    # 5. Metals mining (NB: 1311 E&P is outside these ranges)
    if sic_n is not None and any(lo <= sic_n <= hi for lo, hi in MINING_SIC_RANGES):
        return _Decline(DeclineReason.MINING, f"SIC {sic} (metals/coal mining) — mining NAV is out of scope")

    # 6. Biotech / pre-commercial — financial signature only, never the SIC label.
    latest = _latest_statement(fin)
    if latest is not None and rev < BIOTECH_MAX_TTM_REVENUE:
        rd = latest.rd or 0.0
        near_zero = rev < BIOTECH_NEAR_ZERO_REVENUE
        rd_heavy = rev > 0 and rd / rev > BIOTECH_RD_TO_REVENUE_MIN
        annual_count = len(annual_income_statements(fin))
        years_public = s.years_public if s.years_public is not None else float(annual_count)
        ocf = operating_cash_flows(s)
        recent_ocf = ocf[-BIOTECH_OCF_LOOKBACK_YEARS:]
        sustained_burn = len(recent_ocf) >= 2 and all(v < 0 for v in recent_ocf)
        if (near_zero or rd_heavy) and years_public > BIOTECH_MIN_YEARS_PUBLIC and sustained_burn:
            what = "~zero revenue" if near_zero else f"R&D {rd / rev:.1f}x revenue"
            return _Decline(
                DeclineReason.BIOTECH_PRECOMMERCIAL,
                f"pre-commercial signature: TTM revenue ${rev / 1e6:,.1f}M, {what}, "
                f"public ~{years_public:.0f}y with negative operating cash flow in each of the last "
                f"{len(recent_ocf)} years (rNPV is out of scope)",
            )

    # 7. Insufficient history
    n_years = len(annual_income_statements(fin))
    if n_years < MIN_FISCAL_YEARS:
        return _Decline(
            DeclineReason.INSUFFICIENT_DATA,
            f"only {n_years} fiscal year(s) of usable data (need ≥ {MIN_FISCAL_YEARS}, TTM excluded)",
        )
    return None


# =======================================================================================
# Step 2: model selection candidates
# =======================================================================================


@dataclass
class _Candidate:
    model: ModelType
    score: float
    reasons: list[str] = field(default_factory=list)
    sotp_segments: list[str] | None = None


@dataclass(frozen=True)
class SegmentAnalysis:
    fiscal_year: int | None
    material: list[str]
    opex_available: bool
    spread: float  # max of margin / growth / capex-intensity spreads among material segments
    detail: str


def analyze_segments(fin: NormalizedFinancials) -> SegmentAnalysis:
    segs = [s for s in fin.segments if not s.period.is_ttm]
    if not segs:
        return SegmentAnalysis(None, [], False, 0.0, "no segment data")
    fy = max(s.period.fiscal_year for s in segs)
    latest = [s for s in segs if s.period.fiscal_year == fy]
    prior = {s.segment_name: s for s in segs if s.period.fiscal_year == fy - 1}
    if len(latest) < 2:
        return SegmentAnalysis(fy, [], False, 0.0, f"single reportable segment in FY{fy}")

    consolidated = next(
        (ln.revenue for ln in annual_income_statements(fin) if ln.period.fiscal_year == fy), None
    )
    rev_base = consolidated if consolidated and consolidated > 0 else sum(max(s.revenue, 0) for s in latest)
    oi_base = sum(max(s.operating_income or 0.0, 0.0) for s in latest)

    material: list[SegmentLine] = []
    for s in latest:
        rev_share = s.revenue / rev_base if rev_base > 0 else 0.0
        oi_share = (s.operating_income or 0.0) / oi_base if oi_base > 0 else 0.0
        if rev_share >= SOTP_MIN_SEGMENT_SHARE or oi_share >= SOTP_MIN_SEGMENT_SHARE:
            material.append(s)
    names = [s.segment_name for s in material]
    if len(material) < 2:
        return SegmentAnalysis(fy, names, False, 0.0, f"fewer than 2 material segments in FY{fy}")

    opex_available = all(s.operating_income is not None for s in material)
    margins = [
        s.operating_income / s.revenue for s in material if s.operating_income is not None and s.revenue > 0
    ]
    margin_spread = max(margins) - min(margins) if len(margins) >= 2 else 0.0
    growths = [
        s.revenue / prior[s.segment_name].revenue - 1
        for s in material
        if s.segment_name in prior and prior[s.segment_name].revenue > 0
    ]
    growth_spread = max(growths) - min(growths) if len(growths) >= 2 else 0.0
    intensities = [s.capex / s.revenue for s in material if s.capex is not None and s.revenue > 0]
    capex_spread = max(intensities) - min(intensities) if len(intensities) >= 2 else 0.0
    spread = max(margin_spread, growth_spread, capex_spread)
    detail = (
        f"FY{fy}: {len(material)} material segments ({', '.join(names)}); spreads — margin "
        f"{margin_spread:.1%}, growth {growth_spread:.1%}, capex intensity {capex_spread:.1%}"
    )
    return SegmentAnalysis(fy, names, opex_available, spread, detail)


def _book_debt_ratios(fin: NormalizedFinancials) -> list[float]:
    out = []
    for b in fin.balance_sheets:
        cap = b.total_debt + b.total_equity
        if cap > 0 and b.total_equity > 0:
            out.append(b.total_debt / cap)
    return out


def market_leverage(s: ClassificationSignals) -> float | None:
    b = _latest_balance(s.financials)
    if b is None:
        return None
    mcap = s.company.market_cap_usd
    if mcap and mcap > 0:
        return b.total_debt / (b.total_debt + mcap) if b.total_debt + mcap > 0 else None
    ratios = _book_debt_ratios(s.financials)
    return ratios[-1] if ratios else None


def is_early_stage(s: ClassificationSignals) -> tuple[bool, str]:
    latest = _latest_statement(s.financials)
    if latest is None or latest.revenue <= 0:
        return False, ""
    margin = latest.operating_income / latest.revenue
    growth = revenue_cagr(annual_income_statements(s.financials))
    if margin < 0 and growth is not None and growth > EARLY_STAGE_MIN_GROWTH:
        return True, (
            f"early-stage variant: TTM operating margin {margin:.1%} < 0 with revenue growing "
            f"{growth:.1%}/yr (> {EARLY_STAGE_MIN_GROWTH:.0%})"
        )
    return False, ""


def _evaluate_candidates(s: ClassificationSignals) -> list[_Candidate]:
    fin = s.financials
    c = s.company
    sic = c.sic_code.strip()
    tags = s.present_tags
    rev = ttm_revenue(s) or (_tag_value(s, TAG_REVENUES) or 0.0)
    bal = _latest_balance(fin)
    equity = bal.total_equity if bal else 0.0
    debt = bal.total_debt if bal else 0.0
    cands: list[_Candidate] = []
    financial_matched = False

    # --- Bank ------------------------------------------------------------------------
    bank_tags = (TAG_DEPOSITS in tags and TAG_NII in tags) or bool(fin.bank_data)
    if bank_tags:
        deposits = _tag_value(s, TAG_DEPOSITS)
        nii = _tag_value(s, TAG_NII)
        if fin.bank_data:
            deposits = deposits if deposits is not None else fin.bank_data[-1].total_deposits
            nii = nii if nii is not None else fin.bank_data[-1].net_interest_income
        deposits, nii = deposits or 0.0, nii or 0.0
        dep_ok = equity > 0 and deposits >= BANK_DEPOSITS_TO_EQUITY_MIN * equity
        nii_ok = nii > 0 and (rev <= 0 or nii >= BANK_NII_TO_REVENUE_MIN * rev)
        if dep_ok and nii_ok:
            financial_matched = True
            cands.append(
                _Candidate(
                    ModelType.EXCESS_RETURN,
                    SCORE_BANK,
                    [
                        f"bank: deposits ${deposits / 1e9:,.1f}B ({deposits / equity:.1f}x equity) and net "
                        f"interest income ${nii / 1e9:,.1f}B are material"
                    ],
                )
            )
        else:
            cands.append(
                _Candidate(ModelType.EXCESS_RETURN, 0.35, ["bank tags present but deposits/NII not material"])
            )

    # --- P&C insurer ------------------------------------------------------------------
    if TAG_PREMIUMS in tags or fin.insurer_data:
        prem = _tag_value(s, TAG_PREMIUMS)
        if prem is None and fin.insurer_data:
            prem = fin.insurer_data[-1].net_premiums_earned
        prem = prem or 0.0
        if prem > 0 and (rev <= 0 or prem >= PC_PREMIUMS_TO_REVENUE_MIN * rev):
            financial_matched = True
            share = f" ({prem / rev:.0%} of revenue)" if rev > 0 else ""
            cands.append(
                _Candidate(
                    ModelType.EXCESS_RETURN,
                    SCORE_PC_INSURER,
                    [f"P&C insurer: net premiums earned ${prem / 1e9:,.1f}B{share} are material"],
                )
            )
        else:
            cands.append(_Candidate(ModelType.EXCESS_RETURN, 0.30, ["premiums tag present but not material"]))

    # --- REIT -------------------------------------------------------------------------
    prop = _tag_value(s, TAG_REAL_ESTATE)
    if prop is None and fin.reit_data:
        r = fin.reit_data[-1]
        prop = r.real_estate_investments_gross - r.accumulated_depreciation
    elected = sic in REIT_SICS or bool(_REIT_NAME_RE.search(c.name))
    if prop is not None or elected:
        capital = debt + equity
        prop_ok = prop is not None and capital > 0 and prop >= REIT_PROPERTY_TO_CAPITAL_MIN * capital
        if prop_ok and elected:
            assert prop is not None
            cands.append(
                _Candidate(
                    ModelType.NAV_REIT,
                    SCORE_REIT,
                    [
                        f"REIT: net investment property ${prop / 1e9:,.1f}B ({prop / capital:.0%} of book "
                        f"capital) and REIT election signal (SIC {sic} / name)"
                    ],
                )
            )
        elif prop_ok:
            cands.append(
                _Candidate(
                    ModelType.NAV_REIT, 0.45, ["material investment property but no REIT election signal"]
                )
            )
        else:
            cands.append(
                _Candidate(ModelType.NAV_REIT, 0.30, ["REIT signal but investment property not material"])
            )

    # --- E&P --------------------------------------------------------------------------
    smog = TAG_SMOG in tags or any(
        e.standardized_measure_disc_future_cash_flows is not None for e in fin.ep_data
    )
    if smog:
        if sic in EP_SICS:
            cands.append(
                _Candidate(
                    ModelType.NAV_EP,
                    SCORE_EP,
                    [f"E&P: standardized measure of proved reserves disclosed, SIC {sic}"],
                )
            )
        else:
            # Integrated majors / refiners disclose reserves too, but are better as FCFF.
            cands.append(
                _Candidate(
                    ModelType.NAV_EP,
                    0.55,
                    [f"reserves standardized measure disclosed but SIC {sic} is not pure-play E&P"],
                )
            )
    elif sic in EP_SICS:
        cands.append(
            _Candidate(ModelType.NAV_EP, 0.40, [f"SIC {sic} is E&P but no standardized measure tag"])
        )

    # --- SOTP -------------------------------------------------------------------------
    seg = analyze_segments(fin)
    if len(seg.material) >= 2:
        if seg.opex_available:
            strength = min(1.0, seg.spread / SOTP_STRONG_SPREAD)
            score = SOTP_SCORE_FLOOR + SOTP_SCORE_RANGE * strength
            reasons = [f"SOTP: {seg.detail}"]
            if seg.fiscal_year is not None and seg.fiscal_year < SOTP_ASU_2023_07_FIRST_FY:
                reasons.append(
                    f"segment opex predates ASU 2023-07 (FY{seg.fiscal_year}); coverage may be thin"
                )
            cands.append(_Candidate(ModelType.SOTP, score, reasons, sotp_segments=list(seg.material)))
        else:
            cands.append(
                _Candidate(
                    ModelType.SOTP,
                    0.40,
                    [f"SOTP not viable: segment operating income not disclosed ({seg.detail})"],
                    sotp_segments=list(seg.material),
                )
            )

    # --- FCFE / FCFF ------------------------------------------------------------------
    if financial_matched:
        cands.append(
            _Candidate(ModelType.FCFF, SCORE_FCFF_FOR_FINANCIALS, ["FCFF is a poor fit for a bank/insurer"])
        )
    else:
        lev = market_leverage(s)
        ratios = _book_debt_ratios(fin)
        stable = (
            len(ratios) >= FCFE_MIN_STABILITY_OBS and max(ratios) - min(ratios) <= FCFE_STABILITY_MAX_RANGE
        )
        if lev is not None:
            stab_txt = (
                f"stable book debt ratio (range {max(ratios) - min(ratios):.1%})"
                if stable
                else "debt ratio not demonstrably stable"
            )
            if lev > FCFE_EXTREME_LEVERAGE and stable:
                fcfe_score, why = SCORE_FCFE_EXTREME, f"FCFE: extreme leverage {lev:.0%} with {stab_txt}"
            elif lev > FCFE_LEVERAGE_THRESHOLD and stable:
                fcfe_score, why = (
                    SCORE_FCFE_RUNNER_UP,
                    f"FCFE candidate: high leverage {lev:.0%} with {stab_txt}",
                )
            elif lev > FCFE_LEVERAGE_THRESHOLD:
                fcfe_score, why = 0.40, f"high leverage {lev:.0%} but {stab_txt}"
            else:
                fcfe_score = 0.20 * min(1.0, lev / FCFE_LEVERAGE_THRESHOLD)
                why = f"leverage {lev:.0%} below FCFE threshold {FCFE_LEVERAGE_THRESHOLD:.0%}"
            cands.append(_Candidate(ModelType.FCFE, fcfe_score, [why]))
        cands.append(_Candidate(ModelType.FCFF, SCORE_FCFF_BASE, ["FCFF: core operating company"]))

    return _dedupe_sorted(cands)


_PRIORITY = [
    ModelType.EXCESS_RETURN,
    ModelType.NAV_REIT,
    ModelType.NAV_EP,
    ModelType.SOTP,
    ModelType.FCFE,
    ModelType.FCFF,
]


def _dedupe_sorted(cands: list[_Candidate]) -> list[_Candidate]:
    best: dict[ModelType, _Candidate] = {}
    for cand in cands:
        if cand.model not in best or cand.score > best[cand.model].score:
            best[cand.model] = cand
    # Ties broken by the spec's first-match-wins order.
    return sorted(best.values(), key=lambda x: (-x.score, _PRIORITY.index(x.model)))


def candidate_scores(signals: ClassificationSignals) -> list[tuple[ModelType, float]]:
    """Model-selection candidates, best first, as ``(model, score)``.

    Covers step 2 only (hard declines are not applied here), so the LLM tiebreak trigger can be
    exercised directly.
    """
    return [(c.model, round(c.score, 6)) for c in _evaluate_candidates(signals)]


# =======================================================================================
# Assembly
# =======================================================================================


def _confidence(top: float, second: float | None, window_years: int, n_flags: int) -> float:
    conf = top
    if second is not None:
        gap = top - second
        if gap < AMBIGUITY_WINDOW:
            conf -= (AMBIGUITY_WINDOW - gap) / 2
    if window_years < 5:
        conf -= LOW_HISTORY_CONFIDENCE_PENALTY
    conf -= min(0.10, 0.02 * n_flags)
    return round(min(0.99, max(0.05, conf)), 4)


def build_result(
    s: ClassificationSignals,
    chosen: ModelType,
    candidates: list[_Candidate] | None = None,
    extra_reasons: list[str] | None = None,
    confidence_override: float | None = None,
) -> ClassificationResult:
    """Assemble a ClassificationResult for a chosen model (used by rules and by the LLM tiebreak)."""
    cands = candidates if candidates is not None else _evaluate_candidates(s)
    by_model = {c.model: c for c in cands}
    chosen_c = by_model.get(chosen) or _Candidate(chosen, 0.5, [])
    others = [c for c in cands if c.model != chosen]
    runner = others[0] if others and others[0].score >= RUNNER_UP_MIN_SCORE else None

    reasons = list(chosen_c.reasons)
    early = False
    if chosen == ModelType.FCFF:
        early, why = is_early_stage(s)
        if early:
            reasons.append(why)
    if runner is not None:
        reasons.append(f"runner-up {runner.model}: {'; '.join(runner.reasons)}")
    # Data flags are not reasons for the choice: they are reported with the valuation result,
    # scoped to the historical window. Only the in-window concerns lower the confidence.
    reasons.extend(extra_reasons or [])

    years, window_reason = historical_window_years(chosen, s.company.sic_code, s.financials)
    conf = (
        confidence_override
        if confidence_override is not None
        else _confidence(
            chosen_c.score,
            runner.score if runner else None,
            years,
            len(window_flags(s.financials, years)),
        )
    )
    return ClassificationResult(
        company=s.company,
        recommended_model=chosen,
        confidence=conf,
        reasons=reasons,
        runner_up=runner.model if runner else None,
        decline_reason=None,
        historical_window_years=years,
        window_reason=window_reason,
        sotp_segments=chosen_c.sotp_segments if chosen == ModelType.SOTP else None,
        early_stage_variant=early,
    )


def classify(signals: ClassificationSignals) -> ClassificationResult:
    """Rules-only classification (no LLM). See module docstring for the order."""
    decline = _check_declines(signals)
    if decline is not None:
        available = len(annual_income_statements(signals.financials))
        return ClassificationResult(
            company=signals.company,
            recommended_model=None,
            confidence=DECLINE_CONFIDENCE[decline.reason],
            reasons=[f"declined ({decline.reason}): {decline.explanation}"],
            runner_up=None,
            decline_reason=decline.reason,
            historical_window_years=min(available, 5),
            window_reason="not applicable: classification declined",
        )
    cands = _evaluate_candidates(signals)
    return build_result(signals, cands[0].model, cands)
