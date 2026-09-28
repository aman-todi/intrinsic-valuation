"""Macro/industry reference data contracts (spec §5.4)."""

from pydantic import BaseModel


class DamodaranIndustryData(BaseModel):
    """One Damodaran industry bucket, used as prompt anchors and for beta."""

    industry_name: str
    unlevered_beta: float
    levered_beta: float | None = None
    avg_debt_to_equity: float | None = None  # market D/E, decimal
    avg_effective_tax_rate: float | None = None
    pretax_operating_margin: float | None = None
    sales_to_capital: float | None = None
    revenue_growth_5y: float | None = None
    number_of_firms: int | None = None
    dataset_as_of: str | None = None  # e.g. "2026-01"
