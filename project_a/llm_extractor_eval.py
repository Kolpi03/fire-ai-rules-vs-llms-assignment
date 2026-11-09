"""Measure the optional Gemini field extractor (``natural_language_parser.llm_extract``) on the 50 plain-language cases.

This measures the RAW extractor (no clarification/validation layer applied), so it answers one question:
"if we let the LLM turn text into tax inputs, how often are the inputs right, and does it notice what is missing?"

Scoring, against the hand-written expectations in ``data/natural_language_cases.json``:
* complete cases (29): ``correct`` if every expected field equals the LLM value (monthly amounts x12, as the
  rule parser does); otherwise ``wrong`` (a wrong input would give a wrong tax figure if used unchecked);
* incomplete / ambiguous / contradictory cases (21): ``flagged`` if every must-ask field is null, has a null
  period (money), or is listed in ``ambiguities``; otherwise ``guessed`` (the LLM supplied a value where the
  correct behaviour is to ask).

Real calls only (no mocks); responses are cached under logs/llm_cache, and the stored result is reused unless
LLM_REMEASURE=1. Output: benchmarks/project_a/llm_extractor_eval.json
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from common.llm_client import LLMUnavailable, get_client, llm_status
from common.paths import BENCH_A
from project_a.natural_language_parser import llm_extract
from project_a.nl_evaluation import load_cases

OUT = BENCH_A / "llm_extractor_eval.json"
SCHEMA = {"gross_salary", "age", "annual_rent_paid", "hra_received", "basic_plus_da", "metro_city", "section_80c",
          "section_80ccd_1b", "health_insurance_self_family", "health_insurance_parents", "parents_are_senior",
          "home_loan_interest_self_occupied", "is_resident"}   # fields llm_extract asks for
MONEY = {"gross_salary", "annual_rent_paid", "hra_received", "basic_plus_da", "section_80c", "section_80ccd_1b",
         "health_insurance_self_family", "health_insurance_parents", "home_loan_interest_self_occupied"}


def _prev_latency() -> float | None:
    if OUT.exists():
        prev = json.loads(OUT.read_text())
        return prev.get("median_latency_ms_real_calls") or prev.get("median_latency_ms_incl_cache")
    return None


def _norm(field: str, item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    v = item.get("value")
    if field in MONEY and isinstance(v, (int, float)):
        return round(v * 12) if item.get("period") == "monthly" else round(v)
    return v


def _flagged(field: str, out: dict, ambig: set) -> bool:
    item = out.get(field)
    if field in ambig or not isinstance(item, dict) or item.get("value") is None:
        return True
    return field in MONEY and item.get("period") in (None, "null")


def run() -> dict[str, Any]:
    if OUT.exists() and os.environ.get("LLM_REMEASURE") != "1":
        prev = json.loads(OUT.read_text())
        if str(prev.get("status", "")).startswith("measured"):
            return prev
    st = llm_status()
    if st["mode"] != "real":
        return {"status": "NOT MEASURED - no GEMINI_API_KEY (offline)", "model": st["model"]}
    client = get_client(use_cache=True)
    rows, calls, lat, errors = [], 0, [], 0
    for c in load_cases():
        try:
            t0 = time.perf_counter()
            out = llm_extract(c["text"], client)
            lat.append((time.perf_counter() - t0) * 1000)
            calls += 1
        except (LLMUnavailable, Exception) as exc:  # noqa: BLE001 - recorded
            errors += 1
            rows.append({"id": c["id"], "style": c["style"], "outcome": "error", "detail": str(exc)[:200]})
            continue
        exp = c["expected"]
        ambig = set(out.get("ambiguities") or []) if isinstance(out, dict) else set()
        if exp["complete"]:
            outside = sorted(k for k in exp["fields"] if k not in SCHEMA)
            diffs = {k: (_norm(k, out.get(k)), v) for k, v in exp["fields"].items()
                     if k in SCHEMA and v is not None and _norm(k, out.get(k)) != v}
            rows.append({"id": c["id"], "style": c["style"], "outcome": "correct" if not diffs else "wrong",
                         "detail": "" if not diffs else f"(got, want): {diffs}",
                         "fields_outside_extractor_schema_not_scored": outside})
        else:
            missed = [f for f in exp["must_ask"] if not _flagged(f, out, ambig)]
            rows.append({"id": c["id"], "style": c["style"], "outcome": "flagged" if not missed else "guessed",
                         "detail": "" if not missed else f"supplied values for must-ask fields {missed}"})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    res = {"status": "measured (real Gemini calls; raw extractor, no validation layer)", "model": st["model"],
           "measured_at": time.strftime("%Y-%m-%d %H:%M:%S"), "n": len(rows), "counts": counts,
           "complete_cases_correct": f"{counts.get('correct', 0)}/{sum(1 for c in load_cases() if c['expected']['complete'])}",
           "incomplete_cases_flagged": f"{counts.get('flagged', 0)}/{sum(1 for c in load_cases() if not c['expected']['complete'])}",
           "cases_with_fields_outside_schema": sum(1 for r in rows if r.get("fields_outside_extractor_schema_not_scored")),
           "errors": errors,
           # latency of real (uncached) calls only: cached replays would report ~0 ms
           "median_latency_ms_real_calls": (sorted(lat)[len(lat) // 2] if lat and sorted(lat)[len(lat) // 2] > 50
                                            else _prev_latency()),
           "rows": rows}
    BENCH_A.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    r = run()
    print(json.dumps({k: v for k, v in r.items() if k != "rows"}, indent=1))
