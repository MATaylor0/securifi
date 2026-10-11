"""A small, safe expression language for eligibility criteria, filters and waterfall conditions.

Expressions use Python syntax but are evaluated against a whitelist of node types and
functions - there is no attribute access, no imports and no arbitrary calls. Numbers are
evaluated as ``Decimal`` so ``ltv <= 0.65`` behaves exactly as written in the legal docs.

Examples::

    property_type in ['office', 'industrial', 'logistics'] and ltv <= 0.65
    lien_position == 'first' and not is_defaulted
    loan_stage != 'construction' or presales_coverage >= 1.0
    remaining_term_months >= 6
    events['AMORTISATION_EVENT']
"""

from __future__ import annotations

import ast
import operator
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any, Callable, Mapping

from .money import D


class ExpressionError(ValueError):
    """The expression is syntactically invalid or uses something that is not allowed."""


class MissingField(LookupError):
    """The expression referenced a field that is absent or null in the data."""

    def __init__(self, name: str):
        super().__init__(name)
        self.name = name


def _months_between(a: date, b: date) -> Decimal:
    return Decimal((b.year - a.year) * 12 + (b.month - a.month)) + Decimal(b.day - a.day) / Decimal(30)


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _coalesce(*values: Any) -> Any:
    for v in values:
        if v is not None:
            return v
    return None


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": lambda x, n=0: round(D(x), int(n)),
    "len": len,
    "lower": lambda s: str(s).lower(),
    "upper": lambda s: str(s).upper(),
    "startswith": lambda s, prefix: str(s).startswith(str(prefix)),
    "contains": lambda s, part: str(part) in str(s),
    "date": _to_date,
    "days_between": lambda a, b: Decimal((_to_date(b) - _to_date(a)).days),
    "months_between": lambda a, b: _months_between(_to_date(a), _to_date(b)),
    "coalesce": _coalesce,
    "is_null": lambda v: v is None,
    "decimal": D,
}

_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}
_CMPOPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_ALLOWED = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.BinOp, ast.UnaryOp, ast.Not, ast.USub, ast.UAdd,
    ast.Compare, ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Call,
    ast.IfExp, ast.Subscript, ast.Dict,
    *_BINOPS.keys(), *_CMPOPS.keys(),
)


@lru_cache(maxsize=4096)
def compile_expression(source: str) -> ast.Expression:
    if not source or not source.strip():
        raise ExpressionError("expression is empty")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"invalid syntax: {exc.msg} (col {exc.offset})") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED):
            raise ExpressionError(f"'{type(node).__name__}' is not allowed in expressions")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                name = getattr(node.func, "id", "?")
                raise ExpressionError(f"function '{name}' is not allowed")
            if node.keywords:
                raise ExpressionError("keyword arguments are not supported")
    return tree


def referenced_names(source: str) -> set[str]:
    """Field names an expression reads (excluding function names)."""
    tree = compile_expression(source)
    funcs = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - funcs - {"True", "False", "None"}


def evaluate(source: str, variables: Mapping[str, Any]) -> Any:
    tree = compile_expression(source)
    return _Evaluator(variables).visit(tree.body)


class _Evaluator:
    def __init__(self, variables: Mapping[str, Any]):
        self.vars = variables

    def visit(self, node: ast.AST) -> Any:
        method = getattr(self, f"visit_{type(node).__name__}")
        return method(node)

    def visit_Constant(self, node: ast.Constant) -> Any:
        value = node.value
        if isinstance(value, bool) or value is None or isinstance(value, str):
            return value
        if isinstance(value, int):
            return Decimal(value)
        if isinstance(value, float):
            return Decimal(repr(value))
        raise ExpressionError(f"unsupported constant {value!r}")

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[node.id]
        if node.id not in self.vars or self.vars[node.id] is None:
            raise MissingField(node.id)
        value = self.vars[node.id]
        if isinstance(value, float):
            return Decimal(str(value))
        if isinstance(value, int) and not isinstance(value, bool):
            return Decimal(value)
        return value

    def visit_List(self, node: ast.List) -> list:
        return [self.visit(e) for e in node.elts]

    visit_Tuple = visit_List

    def visit_Set(self, node: ast.Set) -> set:
        return {self.visit(e) for e in node.elts}

    def visit_Dict(self, node: ast.Dict) -> dict:
        return {self.visit(k): self.visit(v) for k, v in zip(node.keys, node.values)}

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:
        if isinstance(node.op, ast.And):
            for value in node.values:
                if not self.visit(value):
                    return False
            return True
        for value in node.values:
            if self.visit(value):
                return True
        return False

    def visit_UnaryOp(self, node: ast.UnaryOp) -> Any:
        operand = self.visit(node.operand)
        if isinstance(node.op, ast.Not):
            return not operand
        if isinstance(node.op, ast.USub):
            return -operand
        return +operand

    def visit_BinOp(self, node: ast.BinOp) -> Any:
        left, right = self.visit(node.left), self.visit(node.right)
        try:
            return _BINOPS[type(node.op)](left, right)
        except (TypeError, ArithmeticError) as exc:
            raise ExpressionError(f"cannot evaluate {ast.unparse(node)}: {exc}") from exc

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            left_cmp, right_cmp = _harmonise(left, right)
            try:
                ok = _CMPOPS[type(op)](left_cmp, right_cmp)
            except TypeError as exc:
                raise ExpressionError(f"cannot compare {ast.unparse(node)}: {exc}") from exc
            if not ok:
                return False
            left = right
        return True

    def visit_IfExp(self, node: ast.IfExp) -> Any:
        return self.visit(node.body) if self.visit(node.test) else self.visit(node.orelse)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        container = self.visit(node.value)
        key = self.visit(node.slice)
        if isinstance(container, Mapping):
            # Unknown keys read as False so flags such as events['EOD'] default to "not active".
            return container.get(key, False)
        try:
            return container[int(key)]
        except (IndexError, TypeError, ValueError) as exc:
            raise ExpressionError(f"cannot index {ast.unparse(node)}") from exc

    def visit_Call(self, node: ast.Call) -> Any:
        func = FUNCTIONS[node.func.id]  # validated at compile time
        args = [self.visit(a) for a in node.args]
        try:
            return func(*args)
        except (TypeError, ValueError) as exc:
            raise ExpressionError(f"{node.func.id}(): {exc}") from exc


def _harmonise(left: Any, right: Any) -> tuple[Any, Any]:
    """Allow date fields (stored as ISO strings) to be compared with date() values."""
    if isinstance(left, date) and isinstance(right, str):
        return left, _to_date(right)
    if isinstance(right, date) and isinstance(left, str):
        return _to_date(left), right
    return left, right
