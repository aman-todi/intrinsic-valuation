"""A tiny spreadsheet-expression DSL: every node renders an Excel formula AND carries its
Python value, so each cell is written with ``write_formula(..., value=...)`` and the cached
value can never drift from the formula text.

    wacc = ctx.n("wacc")                     # Name node (value = twin value of the named cell)
    g = ctx.n("revenue_growth_y5") - (ctx.n("revenue_growth_y5") - gt) * k
    ref = sheet.put(row, col, g)             # writes "=revenue_growth_y5-(...)*..." + cached value

Rendering follows Excel precedence (negation > ^ > * / > + - > comparison) and adds
parentheses only where needed; negative literals are always parenthesised.
Arithmetic on values is "safe": division by zero gives NaN, overflow gives inf, and a
complex power gives NaN, mirroring an Excel error without raising in Python.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from xlsxwriter.utility import xl_rowcol_to_cell

PREC_CMP = 1
PREC_ADD = 3
PREC_MUL = 4
PREC_POW = 5
PREC_NEG = 6
PREC_ATOM = 100

Value = float | bool | str


def _safe(fn: Callable[[], float]) -> float:
    try:
        out = fn()
    except ZeroDivisionError:
        return math.nan
    except OverflowError:
        return math.inf
    except TypeError:  # arithmetic on a string (e.g. "n/a") -> Excel #VALUE!
        return math.nan
    if isinstance(out, complex):
        return math.nan
    return out


class E:
    """Base expression node."""

    prec = PREC_ATOM
    value: Value

    def render(self, sheet: str | None = None) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    # arithmetic -------------------------------------------------------------------
    def __add__(self, other: object) -> E:
        return Bin("+", self, lift(other))

    def __radd__(self, other: object) -> E:
        return Bin("+", lift(other), self)

    def __sub__(self, other: object) -> E:
        return Bin("-", self, lift(other))

    def __rsub__(self, other: object) -> E:
        return Bin("-", lift(other), self)

    def __mul__(self, other: object) -> E:
        return Bin("*", self, lift(other))

    def __rmul__(self, other: object) -> E:
        return Bin("*", lift(other), self)

    def __truediv__(self, other: object) -> E:
        return Bin("/", self, lift(other))

    def __rtruediv__(self, other: object) -> E:
        return Bin("/", lift(other), self)

    def __pow__(self, other: object) -> E:
        return Bin("^", self, lift(other))

    def __rpow__(self, other: object) -> E:
        return Bin("^", lift(other), self)

    def __neg__(self) -> E:
        return Neg(self)

    # comparisons (return Cmp nodes; identity __eq__/__hash__ are left untouched) ------
    def __lt__(self, other: object) -> E:
        return Cmp("<", self, lift(other))

    def __le__(self, other: object) -> E:
        return Cmp("<=", self, lift(other))

    def __gt__(self, other: object) -> E:
        return Cmp(">", self, lift(other))

    def __ge__(self, other: object) -> E:
        return Cmp(">=", self, lift(other))

    @property
    def num(self) -> float:
        """The value as a float (NaN for non-numeric)."""
        v = self.value
        if isinstance(v, bool):
            return float(v)
        if isinstance(v, int | float):
            return float(v)
        return math.nan


def lift(x: object) -> E:
    if isinstance(x, E):
        return x
    if isinstance(x, bool):
        raise TypeError("booleans are not valid literals")
    if isinstance(x, int | float):
        return Num(float(x))
    if isinstance(x, str):
        return Str(x)
    raise TypeError(f"cannot lift {type(x).__name__} into an expression")


class Num(E):
    """A numeric literal. Keep these to structural constants (0, 1); model constants are names."""

    def __init__(self, v: float) -> None:
        self.value = float(v)

    def render(self, sheet: str | None = None) -> str:
        v = float(self.value)
        text = str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
        return f"({text})" if v < 0 else text


class Str(E):
    def __init__(self, s: str) -> None:
        self.value = s

    def render(self, sheet: str | None = None) -> str:
        return '"' + str(self.value).replace('"', '""') + '"'


class Name(E):
    """A workbook defined name."""

    def __init__(self, name: str, value: Value) -> None:
        self.name = name
        self.value = value

    def render(self, sheet: str | None = None) -> str:
        return self.name


class Ref(E):
    """An absolute single-cell reference; sheet-qualified unless rendered on the same sheet."""

    def __init__(self, sheet: str, row: int, col: int, value: Value) -> None:
        self.sheet = sheet
        self.row = row
        self.col = col
        self.value = value

    @property
    def cell(self) -> str:
        return xl_rowcol_to_cell(self.row, self.col, row_abs=True, col_abs=True)

    @property
    def qualified(self) -> str:
        return f"{self.sheet}!{self.cell}"

    def render(self, sheet: str | None = None) -> str:
        return self.cell if sheet == self.sheet else self.qualified


_OPS: dict[str, tuple[int, Callable[[float, float], float]]] = {
    "+": (PREC_ADD, lambda a, b: a + b),
    "-": (PREC_ADD, lambda a, b: a - b),
    "*": (PREC_MUL, lambda a, b: a * b),
    "/": (PREC_MUL, lambda a, b: a / b),
    "^": (PREC_POW, lambda a, b: a**b),
}


class Bin(E):
    def __init__(self, op: str, a: E, b: E) -> None:
        self.op = op
        self.a = a
        self.b = b
        self.prec, fn = _OPS[op]
        self.value = _safe(lambda: fn(a.value, b.value))  # type: ignore[arg-type]

    def render(self, sheet: str | None = None) -> str:
        left = self.a.render(sheet)
        right = self.b.render(sheet)
        # Left operand: wrap lower precedence; for ^ also wrap equal (be explicit).
        if self.a.prec < self.prec or (self.op == "^" and self.a.prec == self.prec):
            left = f"({left})"
        # Right operand: wrap lower precedence; wrap equal for non-associative ops.
        if self.b.prec < self.prec or (self.b.prec == self.prec and self.op in "-/^"):
            right = f"({right})"
        return f"{left}{self.op}{right}"


class Neg(E):
    prec = PREC_NEG

    def __init__(self, a: E) -> None:
        self.a = a
        self.value = _safe(lambda: -a.value)  # type: ignore[operator]

    def render(self, sheet: str | None = None) -> str:
        inner = self.a.render(sheet)
        if self.a.prec < self.prec:
            inner = f"({inner})"
        return f"-{inner}"


_CMP: dict[str, Callable[[float, float], bool]] = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


class Cmp(E):
    prec = PREC_CMP

    def __init__(self, op: str, a: E, b: E) -> None:
        self.op = op
        self.a = a
        self.b = b
        try:
            self.value = bool(_CMP[op](a.value, b.value))  # type: ignore[arg-type]
        except TypeError:
            self.value = False

    def render(self, sheet: str | None = None) -> str:
        left = self.a.render(sheet)
        right = self.b.render(sheet)
        if self.a.prec <= self.prec:
            left = f"({left})"
        if self.b.prec <= self.prec:
            right = f"({right})"
        return f"{left}{self.op}{right}"


class Func(E):
    def __init__(self, name: str, args: Sequence[E], value: Value) -> None:
        self.name = name
        self.args = list(args)
        self.value = value

    def render(self, sheet: str | None = None) -> str:
        return f"{self.name}({','.join(a.render(sheet) for a in self.args)})"


# --------------------------------------------------------------------------- functions


def IF(cond: E, a: object, b: object) -> E:  # noqa: N802 - mirrors the Excel function name
    a_, b_ = lift(a), lift(b)
    return Func("IF", [cond, a_, b_], a_.value if cond.value else b_.value)


def MAX(*args: object) -> E:  # noqa: N802
    xs = [lift(a) for a in args]
    return Func("MAX", xs, _safe(lambda: max(x.value for x in xs)))  # type: ignore[type-var]


def MIN(*args: object) -> E:  # noqa: N802
    xs = [lift(a) for a in args]
    return Func("MIN", xs, _safe(lambda: min(x.value for x in xs)))  # type: ignore[type-var]


def SUM(*args: object) -> E:  # noqa: N802
    xs = [lift(a) for a in args]
    return Func("SUM", xs, _safe(lambda: math.fsum(x.num for x in xs)))


def ABS(x: object) -> E:  # noqa: N802
    x_ = lift(x)
    return Func("ABS", [x_], _safe(lambda: abs(x_.num)))


def excel_round(x: float, digits: int = 0) -> float:
    """Excel ROUND: half away from zero."""
    if math.isnan(x) or math.isinf(x):
        return x
    m = 10.0**digits
    return math.copysign(math.floor(abs(x) * m + 0.5) / m, x)


def ROUND(x: object, digits: int = 0) -> E:  # noqa: N802
    x_ = lift(x)
    return Func("ROUND", [x_, Num(digits)], excel_round(x_.num, digits))


def sum_of(items: Sequence[E]) -> E:
    """``a+b+c`` (plain addition chain; ``0`` for an empty list)."""
    if not items:
        return Num(0)
    out: E = items[0]
    for x in items[1:]:
        out = out + x
    return out


def cached(value: Value) -> float | str:
    """Cached cell value for ``write_formula``: NaN/inf -> 0 (Excel shows its own error)."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, str):
        return value
    if math.isnan(value) or math.isinf(value):
        return 0
    return value
