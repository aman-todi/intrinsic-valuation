"""Headless LibreOffice recalculation + read-back (spec §7.1 mandatory verification, §8.3).

``recalc_and_read`` round-trips the workbook through ``soffice --convert-to xlsx`` using a
throwaway user profile seeded with ``registrymodifications.xcu`` that forces a full
recalculation on load (``OOXMLRecalcMode`` / ``ODFRecalcMode`` = 0, "always recalculate";
note LibreOffice's enum is 0 = always, 1 = never, 2 = prompt — the spec's "=1" would keep
the cached values). The re-saved file is read with ``openpyxl(data_only=True)`` and the
requested defined names are resolved to their recalculated values.

The subprocess is async (``asyncio.create_subprocess_exec``) so a run can be cancelled or
timed out: on timeout or cancellation the soffice process is killed and reaped, and the
temporary profile / output directories are always removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import shutil
import tempfile
from pathlib import Path

from openpyxl import load_workbook

from app.schemas.valuation_result import ValuationResult

SOFFICE_BINARIES = ("soffice", "libreoffice")
DEFAULT_TIMEOUT = 60.0

_RECALC_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry"
           xmlns:xs="http://www.w3.org/2001/XMLSchema"
           xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <item oor:path="/org.openoffice.Office.Calc/Formula/Load">
    <prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop>
  </item>
  <item oor:path="/org.openoffice.Office.Calc/Formula/Load">
    <prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop>
  </item>
</oor:items>
"""


class ExcelRecalcError(RuntimeError):
    """soffice missing / failed / timed out, or a requested name could not be read."""


class ExcelVerificationError(AssertionError):
    """The recalculated workbook disagrees with the engine's value per share."""

    def __init__(self, message: str, *, expected: float, actual: float) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


def find_soffice() -> str | None:
    for name in SOFFICE_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    return None


def seed_recalc_profile(profile_dir: str | Path) -> None:
    """Write a LibreOffice user profile that always recalculates formulas on load."""
    user = Path(profile_dir) / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "registrymodifications.xcu").write_text(_RECALC_XCU, encoding="utf-8")


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()


async def recalc_file(xlsx_path: str | Path, out_dir: str | Path, timeout: float = DEFAULT_TIMEOUT) -> Path:
    """Recalculate ``xlsx_path`` with headless LibreOffice, writing the result into ``out_dir``."""
    soffice = find_soffice()
    if soffice is None:
        raise ExcelRecalcError("LibreOffice (soffice) is not on PATH")
    src = Path(xlsx_path).resolve()
    if not src.is_file():
        raise ExcelRecalcError(f"workbook not found: {src}")
    profile_dir = tempfile.mkdtemp(prefix="lo-profile-")
    try:
        seed_recalc_profile(profile_dir)
        proc = await asyncio.create_subprocess_exec(
            soffice,
            "--headless",
            "--norestore",
            "--nolockcheck",
            "--nodefault",
            f"-env:UserInstallation={Path(profile_dir).as_uri()}",
            "--convert-to",
            "xlsx:Calc MS Excel 2007 XML",
            "--outdir",
            str(out_dir),
            str(src),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "SAL_USE_VCLPLUGIN": "svp"},
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError as exc:
            await _kill(proc)
            raise ExcelRecalcError(f"soffice timed out after {timeout:.0f}s") from exc
        except asyncio.CancelledError:
            await _kill(proc)
            raise
        out = Path(out_dir) / (src.stem + ".xlsx")
        if proc.returncode != 0 or not out.is_file():
            detail = (stderr or b"").decode(errors="replace").strip() or (stdout or b"").decode(
                errors="replace"
            )
            raise ExcelRecalcError(f"soffice conversion failed (exit {proc.returncode}): {detail[:500]}")
        return out
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def read_defined_names(xlsx_path: str | Path, names: list[str]) -> dict[str, float]:
    """Cached values of workbook-global defined names (single cells) as floats."""
    wb = load_workbook(str(xlsx_path), data_only=True)
    try:
        out: dict[str, float] = {}
        for name in names:
            dn = wb.defined_names.get(name)
            if dn is None:
                raise ExcelRecalcError(f"defined name {name!r} not found in workbook")
            dests = list(dn.destinations)
            if len(dests) != 1:
                raise ExcelRecalcError(f"defined name {name!r} does not resolve to one range")
            sheet, coord = dests[0]
            value = wb[sheet][coord.replace("$", "")].value
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ExcelRecalcError(f"defined name {name!r} is not numeric after recalculation: {value!r}")
            out[name] = float(value)
        return out
    finally:
        wb.close()


async def recalc_and_read(
    xlsx_path: str | Path, names: list[str], timeout: float = DEFAULT_TIMEOUT
) -> dict[str, float]:
    """Force a LibreOffice recalculation of ``xlsx_path`` and return ``{name: value}``."""
    out_dir = tempfile.mkdtemp(prefix="lo-recalc-")
    try:
        recalced = await recalc_file(xlsx_path, out_dir, timeout=timeout)
        return await asyncio.to_thread(read_defined_names, recalced, names)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def relative_error(actual: float, expected: float) -> float:
    """|actual - expected| / max(|expected|, 1): relative, but absolute (in $) when |expected| < 1."""
    if math.isnan(actual):
        return math.inf
    return abs(actual - expected) / max(abs(expected), 1.0)


async def verify_workbook(
    xlsx_path: str | Path,
    result: ValuationResult,
    tolerance: float = 0.005,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, float]:
    """Recalculate and check ``value_per_share`` against the engine; returns the values read.

    Raises ``ExcelVerificationError`` when the relative error exceeds ``tolerance`` (an
    absolute tolerance in $ when |value per share| < 1), ``ExcelRecalcError`` when
    LibreOffice fails.
    """
    values = await recalc_and_read(xlsx_path, ["value_per_share"], timeout=timeout)
    actual = values["value_per_share"]
    expected = result.value_per_share
    err = relative_error(actual, expected)
    if err > tolerance:
        raise ExcelVerificationError(
            f"Excel value per share {actual:.6f} differs from engine {expected:.6f} "
            f"(error {err:.4%} > {tolerance:.2%})",
            expected=expected,
            actual=actual,
        )
    return values
