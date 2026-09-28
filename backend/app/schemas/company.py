"""Company snapshot and classification contracts (spec §4.1)."""

from enum import StrEnum

from pydantic import BaseModel, Field


class ModelType(StrEnum):
    FCFF = "fcff"
    FCFE = "fcfe"
    EXCESS_RETURN = "excess_return"
    NAV_REIT = "nav_reit"
    NAV_EP = "nav_ep"
    SOTP = "sotp"


class DeclineReason(StrEnum):
    BIOTECH_PRECOMMERCIAL = "biotech_precommercial"
    LIFE_INSURER = "life_insurer"
    SPAC_OR_TRUST = "spac_or_trust"
    MLP = "mlp"
    MINING = "mining"
    NON_10K_FILER = "non_10k_filer"  # 20-F/40-F filers etc.
    INSUFFICIENT_DATA = "insufficient_data"  # <3 years of usable history


class CompanySnapshot(BaseModel):
    ticker: str
    cik: str
    name: str
    sic_code: str
    sic_description: str
    market_cap_usd: float
    share_class_note: str | None = None  # e.g. "Class A (GOOGL); Class C (GOOG) also trades"


class ClassificationResult(BaseModel):
    company: CompanySnapshot
    recommended_model: ModelType | None  # None if declined
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]
    runner_up: ModelType | None
    decline_reason: DeclineReason | None
    historical_window_years: int  # 5 or 10 (or fewer, with a confidence flag)
    window_reason: str
    sotp_segments: list[str] | None = None  # populated only when recommended_model == SOTP
    early_stage_variant: bool = False  # FCFF early-stage-tech variant (negative margin, >20% growth)
