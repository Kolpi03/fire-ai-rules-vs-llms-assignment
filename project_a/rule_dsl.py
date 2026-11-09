"""Restricted expression language used by LLM-compiled rule artifacts (Engine C).

Why a DSL instead of letting the LLM write Python?
  * Safety: compiled artifacts are *data*. They are parsed with :mod:`ast` and only a whitelist of
    node types / function names is executed — no imports, attributes, subscripts, loops or lambdas.
  * Reviewability: each step is one line ``output = expression`` tagged with clause + passage id,
    which a reviewer can check against the statute line by line.
  * Determinism: evaluation is pure ``Decimal`` arithmetic.

Grammar (Python expression syntax subset)
  literals:  integers, decimals (``0.05``), strings (only as table names), True/False
  names:     taxpayer input fields and outputs of earlier steps
  operators: + - * /, comparisons, and/or/not, ``a if cond else b``
  functions: min(...), max(...), slab_tax(income, 'table_name'), round10(x)
"""
from __future__ import annotations

import ast
from decimal import Decimal
from typing import Any, Mapping

D = Decimal
ALLOWED_FUNCS = {"min", "max", "slab_tax", "round10"}
_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call,
                  ast.Name, ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.USub, ast.UAdd,
                  ast.Not, ast.And, ast.Or, ast.Gt, ast.GtE, ast.Lt, ast.LtE, ast.Eq, ast.NotEq)


class DSLError(ValueError):
    pass


def parse(expr: str) -> ast.Expression:
    """Parse and whitelist-check an expression (raises DSLError on anything not allowed)."""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise DSLError(f"syntax error in {expr!r}: {exc}") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise DSLError(f"disallowed syntax {type(node).__name__} in {expr!r}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in ALLOWED_FUNCS:
                raise DSLError(f"disallowed function in {expr!r}")
            if node.keywords:
                raise DSLError("keyword arguments are not allowed")
    return tree


def names_used(expr: str) -> set[str]:
    tree = parse(expr)
    called = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call)}
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - called


def round10(x: Decimal) -> Decimal:
    n = int(x)
    return D(n - n % 10 + (10 if n % 10 >= 5 else 0))


def slab_tax(income: Decimal, table: list[list[Any]]) -> Decimal:
    tax = D(0)
    for lo, hi, rate in table:
        if income <= D(lo):
            break
        top = income if hi is None else min(income, D(hi))
        tax += (top - D(lo)) * D(str(rate))
    return tax


def evaluate(expr: str | ast.Expression, env: Mapping[str, Any], tables: Mapping[str, list]) -> Any:
    tree = parse(expr) if isinstance(expr, str) else expr

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool) or isinstance(v, str):
                return v
            if isinstance(v, (int, float)):
                return D(str(v))
            raise DSLError(f"unsupported constant {v!r}")
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise DSLError(f"unknown variable {node.id!r}")
            v = env[node.id]
            return D(v) if isinstance(v, int) and not isinstance(v, bool) else v
        if isinstance(node, ast.UnaryOp):
            v = ev(node.operand)
            return (not v) if isinstance(node.op, ast.Not) else (-v if isinstance(node.op, ast.USub) else v)
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Add):
                return a + b
            if isinstance(node.op, ast.Sub):
                return a - b
            if isinstance(node.op, ast.Mult):
                return a * b
            return a / b
        if isinstance(node, ast.BoolOp):
            vals = [ev(v) for v in node.values]
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.Compare):
            left = ev(node.left)
            for op, comp in zip(node.ops, node.comparators):
                right = ev(comp)
                ok = {ast.Gt: left > right, ast.GtE: left >= right, ast.Lt: left < right,
                      ast.LtE: left <= right, ast.Eq: left == right, ast.NotEq: left != right}[type(op)]
                if not ok:
                    return False
                left = right
            return True
        if isinstance(node, ast.IfExp):
            return ev(node.body) if ev(node.test) else ev(node.orelse)
        if isinstance(node, ast.Call):
            fn = node.func.id  # type: ignore[attr-defined]
            args = [ev(a) for a in node.args]
            if fn == "min":
                return min(args)
            if fn == "max":
                return max(args)
            if fn == "round10":
                return round10(args[0])
            if fn == "slab_tax":
                if len(args) != 2 or not isinstance(args[1], str) or args[1] not in tables:
                    raise DSLError(f"slab_tax needs a known table name, got {args[1:]!r}")
                return slab_tax(args[0], tables[args[1]])
        raise DSLError(f"cannot evaluate {ast.dump(node)[:80]}")

    return ev(tree)
