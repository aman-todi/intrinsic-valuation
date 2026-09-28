"""Regenerate the small Damodaran-layout fixture workbooks in this directory.

Run from backend/:  .venv/bin/python tests/fixtures/damodaran/generate_fixtures.py

The layouts mirror Damodaran's real files: a FAQ sheet first, then a data sheet with banner rows
above the header row ("Industry Name" / "Year"), and footnote rows below the table.
"""

from pathlib import Path

from openpyxl import Workbook

HERE = Path(__file__).parent


def _banner(ws, title: str) -> None:
    ws.append([title])
    ws.append(["Date updated:", "5-Jan-26"])
    ws.append(["Created by:", "Aswath Damodaran, adamodar@stern.nyu.edu"])
    ws.append(["What is this data?", "US companies"])
    ws.append(["Home Page:", "http://www.damodaran.com"])
    ws.append([])
    ws.append([])


def betas() -> None:
    wb = Workbook()
    faq = wb.active
    faq.title = "Variables & FAQ"
    faq.append(["Variable", "Definition"])
    faq.append(["Industry Name", "Damodaran industry classification"])
    ws = wb.create_sheet("Industry Averages")
    _banner(ws, "Betas by Sector (US)")
    ws.append(
        [
            "Industry Name",
            "Number of firms",
            "Beta ",
            "D/E Ratio",
            "Effective Tax rate",
            "Unlevered beta",
            "Cash/Firm value",
            "Unlevered beta corrected for cash",
            "HiLo Risk",
            "Standard deviation of equity",
            "Standard deviation in operating income (last 10 years)",
        ]
    )
    rows = [
        ("Bank (Money Center)", 15, 0.9212, 1.4531, 0.1802, 0.4301, 0.1201, 0.4888, 0.2, 0.25, 0.12),
        ("Computers/Peripherals", 40, 1.1805, 0.0412, 0.1011, 1.1391, 0.0301, 1.1745, 0.3, 0.35, 0.2),
        ("R.E.I.T.", 190, 0.9003, 0.7512, 0.0205, 0.5210, 0.0110, 0.5268, 0.25, 0.3, 0.15),
        ("Rubber & Tires", 3, 1.05, 1.40, 0.10, 0.4955, 0.02, 0.5056, 0.3, 0.4, 0.2),
        (
            "Software (System & Application)",
            350,
            1.2011,
            0.0398,
            0.0702,
            1.1580,
            0.0402,
            1.2065,
            0.4,
            0.45,
            0.2,
        ),
        ("Total Market", 5900, 1.0, 0.25, 0.12, 0.83, 0.03, 0.855, 0.35, 0.4, 0.2),
    ]
    for r in rows:
        ws.append(list(r))
    ws.append([])
    ws.append(["Data source: S&P Capital IQ, Bloomberg and Value Line"])
    wb.save(HERE / "betas.xlsx")


def margin() -> None:
    wb = Workbook()
    wb.active.title = "Variables & FAQ"
    ws = wb.create_sheet("Industry Averages")
    _banner(ws, "Operating and Net Margins by Industry Sector")
    ws.append(
        [
            "Industry Name",
            "Number of firms",
            "Gross Margin",
            "Net Margin",
            "Pre-tax, Pre-stock compensation Operating Margin",
            "Pre-tax Unadjusted Operating Margin",
            "After-tax Unadjusted Operating Margin",
            "Pre-tax Lease adjusted Margin",
            "After-tax Lease Adjusted Margin",
            "Pre-tax Lease & R&D adj Margin",
        ]
    )
    rows = [
        ("Bank (Money Center)", 15, "NA", 0.28, 0.36, 0.3312, 0.27, 0.34, 0.28, 0.34),
        ("Computers/Peripherals", 40, 0.44, 0.24, 0.30, 0.2655, 0.23, 0.27, 0.24, 0.33),
        ("R.E.I.T.", 190, 0.60, 0.25, 0.31, 0.3001, 0.29, 0.31, 0.3, 0.31),
        ("Software (System & Application)", 350, 0.72, 0.25, 0.38, 0.3220, 0.28, 0.33, 0.29, 0.42),
        ("Total Market", 5900, 0.36, 0.09, 0.14, 0.1210, 0.10, 0.13, 0.11, 0.15),
    ]
    for r in rows:
        ws.append(list(r))
    ws.append(["Footnote: margins are aggregated (sum of operating income / sum of revenues)"])
    wb.save(HERE / "margin.xlsx")


def histimpl() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Historical Impl Premiums"
    ws.append(["Historical Implied Equity Risk Premiums for the S&P 500"])
    ws.append(["Updated in January 2026"])
    ws.append([])
    ws.append(
        [
            "Year",
            "Earnings Yield",
            "Dividend Yield",
            "S&P 500",
            "Earnings*",
            "Dividends*",
            "Dividends + Buybacks",
            "Change in Earnings",
            "Change in Dividends",
            "T.Bond Rate",
            "Bond-Bill",
            "Smoothed Growth",
            "Implied Premium (DDM)",
            "Analyst Growth Estimate",
            "Implied Premium (FCFE)",
            "Implied Premium (FCFE with sustainable Payout)",
        ]
    )
    rows = [
        (
            2022,
            0.0582,
            0.0172,
            3839.5,
            223.5,
            66.0,
            162.0,
            -0.0588,
            0.098,
            0.0388,
            0.0,
            0.05,
            0.0351,
            0.08,
            0.0594,
            0.0561,
        ),
        (
            2023,
            0.0444,
            0.0147,
            4769.8,
            211.5,
            70.1,
            155.9,
            -0.0539,
            0.062,
            0.0388,
            0.0,
            0.05,
            0.0288,
            0.07,
            0.0460,
            0.0435,
        ),
        (
            2024,
            0.0400,
            0.0127,
            5881.6,
            235.0,
            74.2,
            174.9,
            0.111,
            0.058,
            0.0458,
            0.0,
            0.05,
            0.0266,
            0.07,
            0.0433,
            0.0410,
        ),
        (
            2025,
            0.0380,
            0.0118,
            6850.0,
            260.2,
            79.0,
            190.1,
            0.107,
            0.065,
            0.0417,
            0.0,
            0.05,
            0.0255,
            0.07,
            0.0423,
            0.0401,
        ),
    ]
    for r in rows:
        ws.append(list(r))
    ws.append([])
    ws.append(["* Trailing 12-month earnings and dividends"])
    wb.save(HERE / "histimpl.xlsx")


if __name__ == "__main__":
    betas()
    margin()
    histimpl()
