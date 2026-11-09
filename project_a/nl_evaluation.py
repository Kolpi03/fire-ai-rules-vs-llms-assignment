"""Evaluate a parser against the 50 hand-written plain-language cases.

Outcome per case:
* ``correct``       — complete case: profile produced with every expected field exact (and unlisted money
                      fields 0); clarify case: no profile AND every must-ask field asked.
* ``over_asked``    — complete case, but the parser asked a question instead (safe failure: no wrong tax).
* ``under_asked``   — clarify case: no profile but a required question is missing (safe-ish).
* ``unsafe``        — a profile was produced that is wrong, or produced when it should have asked
                      (this is the failure that leads to a confidently wrong tax figure).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from common.paths import PA_DATA
from project_a.models import TaxpayerProfile
from project_a.natural_language_parser import ParseResult, parse

DEFAULTS = {f: (info.default if info.default is not None else None)
            for f, info in TaxpayerProfile.model_fields.items()}


def load_cases() -> list[dict[str, Any]]:
    return json.loads((PA_DATA / "natural_language_cases.json").read_text(encoding="utf-8"))["cases"]


def judge(case: dict[str, Any], res: ParseResult) -> tuple[str, str]:
    exp = case["expected"]
    asked = {c.field for c in res.clarifications}
    if exp["complete"]:
        if res.profile is None:
            return "over_asked", f"asked {sorted(asked)}"
        got = res.profile.model_dump()
        want = {**{k: v for k, v in DEFAULTS.items() if k not in ("age", "gross_salary")}, **exp["fields"]}
        if "financial_year" in exp:
            want["financial_year"] = exp["financial_year"]
        diffs = {k: (got.get(k), v) for k, v in want.items() if v is not None and got.get(k) != v}
        return ("correct", "") if not diffs else ("unsafe", f"field diffs (got, want): {diffs}")
    if res.profile is not None:
        return "unsafe", "produced a profile instead of asking"
    missing = set(exp["must_ask"]) - asked
    return ("correct", "") if not missing else ("under_asked", f"missing questions {sorted(missing)}; asked {sorted(asked)}")


def evaluate(parser: Callable[[str], ParseResult] = parse) -> dict[str, Any]:
    rows = []
    for c in load_cases():
        res = parser(c["text"])
        outcome, detail = judge(c, res)
        rows.append({"id": c["id"], "style": c["style"], "outcome": outcome, "detail": detail,
                     "questions": [q.question for q in res.clarifications]})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    return {"n": len(rows), "counts": counts, "accuracy": counts.get("correct", 0) / len(rows), "rows": rows}


if __name__ == "__main__":
    out = evaluate()
    print(json.dumps(out["counts"], indent=1), f"accuracy={out['accuracy']:.2%}")
    for r in out["rows"]:
        if r["outcome"] != "correct":
            print(r["id"], r["style"], r["outcome"], r["detail"])
