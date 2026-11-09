"""Render a TaxComputation (plus optional parse result) as a plain-text explanation for the user.

Deterministic templating, not an LLM: the explanation must say exactly what the engine computed, with
the clause for every line, and must never introduce numbers of its own.
"""
from __future__ import annotations

from decimal import Decimal

from project_a.models import EngineStatus, RegimeResult, TaxComputation
from project_a.natural_language_parser import FieldStatus, ParseResult


def _rs(x: Decimal) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}Rs {abs(x):,.2f}"


def regime_block(r: RegimeResult, title: str) -> list[str]:
    out = [f"{title}", "-" * len(title)]
    for line in r.lines:
        out.append(f"  {line.name:<44} {_rs(line.amount):>18}   [{line.clause}; {line.rule_id} v{line.rule_version}]")
        if line.explanation:
            out.append(f"      {line.explanation}")
    out.append(f"  {'TAX PAYABLE':<44} {_rs(r.tax_payable):>18}")
    return out


def explain(c: TaxComputation, parsed: ParseResult | None = None) -> str:
    if c.status == EngineStatus.UNAVAILABLE:
        return f"[{c.engine}] unavailable: {'; '.join(c.notes)}"
    lines = [f"Income-tax computation for FY {c.financial_year} (engine: {c.engine}, rule set {c.rule_set_id} "
             f"v{c.rule_set_version}, status: {c.status.value})", ""]
    if c.old:
        lines += regime_block(c.old, "OLD REGIME") + [""]
    if c.new:
        lines += regime_block(c.new, "NEW REGIME (Section 115BAC)") + [""]
    if c.recommended_regime and c.old and c.new:
        other = "old" if c.recommended_regime == "new" else "new"
        if c.savings:
            lines.append(f"RECOMMENDATION: choose the {c.recommended_regime.upper()} regime — it saves {_rs(c.savings)} "
                         f"compared with the {other} regime.")
        else:
            lines.append("RECOMMENDATION: both regimes give the same tax; the NEW regime is the default, so no "
                         "opt-out is needed.")
    for n in c.notes:
        if n:
            lines.append(f"Note: {n}")
    if parsed is not None:
        assumed = [k for k, v in parsed.fields.items() if v.status in (FieldStatus.ASSUMED, FieldStatus.NOT_MENTIONED)
                   and k not in ("spouse_is_senior",)]
        derived = [f"{k} ({v.note})" for k, v in parsed.fields.items() if v.status == FieldStatus.DERIVED]
        if assumed:
            lines.append("Assumed because not mentioned (tell us if any is wrong): " + ", ".join(assumed))
        if derived:
            lines.append("Derived: " + "; ".join(derived))
    return "\n".join(lines)
