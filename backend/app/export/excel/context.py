"""Shared build context: workbook, formats, defined-name registry and a cell writer.

Defined-name conventions (spec §7.1; see ``builder`` module docstring for the full list):
    * every AssumptionField -> its field name (``terminal_growth_rate``); SOTP segment
      fields -> ``seg{i}_<field>`` (1-based, input order); SOTP consolidated FCFF
      cross-check fields -> ``cons_<field>``
    * market / financial-statement inputs -> descriptive snake_case (``market_price``,
      ``diluted_shares``, ``total_debt`` ...)
    * engine constants -> snake_case (``min_rate_growth_spread``, ``scenario_growth_delta`` ...)
    * outputs -> ``operating_value``, ``enterprise_value``, ``equity_value``,
      ``value_per_share``, ``upside_pct``, ``scenario_{bull,bear}_value_per_share``,
      ``sens_row{i}_col{j}`` (0-based, row-major)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from xlsxwriter.workbook import Workbook
from xlsxwriter.worksheet import Worksheet

from app.export.excel.expr import E, Name, Num, Ref, Str, cached
from app.schemas.financials import MarketSnapshot, NormalizedFinancials
from app.schemas.valuation_result import ValuationResult

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_CELL_LIKE = re.compile(r"^[A-Za-z]{1,3}\d+$")
_R1C1_LIKE = re.compile(r"^[RrCc]\d*([RrCc]\d*)?$")


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name) or _CELL_LIKE.match(name) or _R1C1_LIKE.match(name):
        raise ValueError(f"invalid workbook defined name: {name!r}")
    return name


class Formats:
    """Named cell formats (built once per workbook)."""

    def __init__(self, wb: Workbook) -> None:
        base = {"font_name": "Calibri", "font_size": 10}
        inp = {"font_color": "#1F4E9E", "bg_color": "#FFF7D6", "border": 1, "border_color": "#D9D9D9"}

        def fmt(**kw: Any):
            return wb.add_format({**base, **kw})

        self.title = fmt(bold=True, font_size=14)
        self.subtitle = fmt(italic=True, font_color="#595959")
        self.header = fmt(bold=True, bg_color="#1F3864", font_color="#FFFFFF", border=1)
        self.header_hl = fmt(bold=True, bg_color="#C65911", font_color="#FFFFFF", border=1)
        self.section = fmt(bold=True, font_size=11, bottom=1)
        self.label = fmt()
        self.bold = fmt(bold=True)
        self.text = fmt(text_wrap=True, valign="top")
        self.note = fmt(italic=True, font_color="#595959", text_wrap=True, valign="top")
        self.warn = fmt(bold=True, font_color="#C00000")
        self.pct = fmt(num_format="0.00%")
        self.money = fmt(num_format="#,##0;(#,##0)")
        self.money2 = fmt(num_format="#,##0.00;(#,##0.00)")
        self.per_share = fmt(num_format="$#,##0.00;($#,##0.00)")
        self.mult = fmt(num_format='0.00"x"')
        self.num = fmt(num_format="0.0000")
        self.int = fmt(num_format="0")
        self.factor = fmt(num_format="0.000000")
        self.pct_in = fmt(num_format="0.00%", **inp)
        self.money_in = fmt(num_format="#,##0.00;(#,##0.00)", **inp)
        self.num_in = fmt(num_format="0.0000", **inp)
        self.mult_in = fmt(num_format='0.00"x"', **inp)
        self.int_in = fmt(num_format="0.00", **inp)
        self.derived_in = fmt(num_format="0.0000", font_color="#7F7F7F", bg_color="#EDEDED", border=1)
        self.pct_hl = fmt(num_format="0.00%", bg_color="#FCE4D6")
        self.money_hl = fmt(num_format="#,##0;(#,##0)", bg_color="#FCE4D6")
        self.money2_hl = fmt(num_format="#,##0.00;(#,##0.00)", bg_color="#FCE4D6")
        self.per_share_bold = fmt(num_format="$#,##0.00;($#,##0.00)", bold=True, bg_color="#E2EFDA", border=1)
        self.money_bold = fmt(num_format="#,##0;(#,##0)", bold=True, top=1)
        self.pct_bold = fmt(num_format="0.00%", bold=True)

    def for_kind(self, kind: str, *, input_cell: bool = False):
        table = {
            "pct": (self.pct, self.pct_in),
            "money": (self.money, self.money_in),
            "money2": (self.money2, self.money_in),
            "per_share": (self.per_share, self.money_in),
            "mult": (self.mult, self.mult_in),
            "num": (self.num, self.num_in),
            "years": (self.num, self.int_in),
            "factor": (self.factor, self.num_in),
        }
        pair = table.get(kind, (self.num, self.num_in))
        return pair[1] if input_cell else pair[0]


class SheetWriter:
    """Writes values / expression formulas to one worksheet and returns ``Ref`` nodes."""

    def __init__(self, ctx: BuildContext, ws: Worksheet) -> None:
        self.ctx = ctx
        self.ws = ws
        self.name = ws.name

    def put(self, row: int, col: int, e: E | float | int | str, fmt=None) -> Ref:
        """Write a formula (any non-literal ``E``) with its cached value, or a plain value."""
        if isinstance(e, E) and not isinstance(e, Num | Str):
            self.ws.write_formula(row, col, "=" + e.render(self.name), fmt, cached(e.value))
            return Ref(self.name, row, col, e.value)
        value = e.value if isinstance(e, Num | Str) else e
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            raise ValueError(f"refusing to write non-finite constant at {self.name}!R{row}C{col}")
        self.ws.write(row, col, value, fmt)
        return Ref(self.name, row, col, value)

    def text(self, row: int, col: int, s: str, fmt=None) -> None:
        self.ws.write_string(row, col, s, fmt)

    def row_label(self, row: int, label: str, fmt=None, col: int = 0) -> None:
        self.ws.write_string(row, col, label, fmt or self.ctx.fmt.label)


@dataclass
class BuildContext:
    wb: Workbook
    fmt: Formats
    result: ValuationResult
    financials: NormalizedFinancials
    market: MarketSnapshot
    assumptions: Any
    company_name: str = ""
    model_reason: str = ""
    sheets: dict[str, SheetWriter] = field(default_factory=dict)
    names: dict[str, Name] = field(default_factory=dict)
    name_refs: dict[str, Ref] = field(default_factory=dict)
    # Model-specific handles written by one sheet and consumed by another.
    refs: dict[str, Any] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return self.result.model_type

    def sheet(self, name: str) -> SheetWriter:
        return self.sheets[name]

    def add_sheet(self, name: str) -> SheetWriter:
        sw = SheetWriter(self, self.wb.add_worksheet(name))
        self.sheets[name] = sw
        return sw

    def define(self, name: str, ref: Ref) -> Name:
        """Create a workbook-global defined name for ``ref`` and return its ``Name`` node."""
        validate_name(name)
        if name in self.names:
            raise ValueError(f"defined name {name!r} already exists")
        self.wb.define_name(name, f"={ref.qualified}")
        node = Name(name, ref.value)
        self.names[name] = node
        self.name_refs[name] = ref
        return node

    def n(self, name: str) -> Name:
        """The ``Name`` node for an existing defined name."""
        return self.names[name]

    def has(self, name: str) -> bool:
        return name in self.names
