"""One-off LLM rule compilation for Engine C: compile -> validate -> verify -> human review -> approve.

Pipeline
--------
1. ``build_prompt(fy)`` puts the official passages for the year (``rule_texts/fy*.yaml``) and the DSL
   specification into one prompt. The prompt is saved next to the artifact for audit.
2. ``compile_with_llm(fy)`` sends it to Gemini once and stores the raw JSON as a *candidate*
   (``compiled_rules/candidates/``) with model, token usage and prompt hash.
3. ``validate_artifact`` statically checks the candidate: every expression parses under the DSL
   whitelist, only refers to inputs/earlier outputs, every step cites a clause and an existing
   passage id, and all required outputs exist.
4. ``verify_against_suite`` runs the candidate on every test case of that financial year and reports
   rupee-exact agreement. A candidate with any mismatch cannot be approved.
5. ``approve`` (human step, via ``python -m project_a.cli compile approve``) records reviewer, date,
   verification result and the artifact hash, and promotes the file to
   ``compiled_rules/fy<YYYY_YY>.compiled.json``. Engine C refuses to run unapproved artifacts.

At runtime Engine C never calls an LLM.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from common.llm_client import get_client
from common.paths import PA_COMPILED, PA_DATA, PA_RULE_TEXTS
from project_a import rule_dsl
from project_a.models import DERIVED_80D_INPUTS, TaxpayerProfile

INPUT_FIELDS = sorted((TaxpayerProfile.model_fields.keys() - {"financial_year"}) | set(DERIVED_80D_INPUTS))
REQUIRED_OUTPUTS = ["total_income", "tax_on_income", "rebate_87a", "surcharge", "cess", "total_tax", "tax_payable"]

DSL_SPEC = f"""
You are compiling Indian income-tax law into an executable rule artifact. Output ONE JSON object:
{{
  "financial_year": "<YYYY-YY>",
  "tables": {{"<table_name>": [[lower, upper_or_null, "rate_decimal_string"], ...], ...}},
  "regimes": {{
    "old": {{"steps": [STEP, ...]}},
    "new": {{"steps": [STEP, ...]}}
  }},
  "regime_choice": {{"tie_breaker": "old|new", "clause": "...", "passage_id": "..."}}
}}
STEP = {{"id": "<REGIME>.<NAME>", "output": "<variable_name>", "expr": "<expression>",
         "clause": "<statutory clause>", "passage_id": "<id of the passage you used>",
         "line": "<human label if this amount should appear in the explanation, else null>"}}

Expression language (Python expression syntax subset; NOTHING else is allowed):
- numbers (use decimals such as 0.05, never percentages), strings only as slab-table names, True/False
- variables: taxpayer inputs {INPUT_FIELDS} and outputs of EARLIER steps of the same regime
- operators: + - * / < <= > >= == != and or not, and `a if condition else b`
- functions: min(a, b, ...), max(a, b, ...), slab_tax(income, 'table_name'), round10(x)
  slab_tax applies progressive rates: table rows [lower, upper, rate]; upper null = no upper bound.
  round10 = Sections 288A/288B: drop paise, round to nearest 10 rupees (5 rounds up).
All money inputs are annual rupees. Booleans: is_resident, metro_city, spouse_is_senior, parents_are_senior,
self_insured, spouse_insured, parent_a_is_senior, parent_a_insured, parent_b_is_senior, parent_b_insured.
80D medical expenditure is given per person (medical_expenditure_self/_spouse/_parent_a/_parent_b, with that person's
seniority and *_insured flag) or, legacy, per group (medical_expenditure_self_family/_parents: no per-person detail).
Derived booleans supplied by the runtime: insured_senior_self_family / insured_senior_parents = a health-insurance
premium in that group is paid for a senior citizen (s.80D(4)); with group-only input a group premium covers everyone.
Each regime MUST define, in this order of dependency, these outputs: {REQUIRED_OUTPUTS}.
total_income must be rounded with round10; tax_payable = round10(total_tax).
Scope: salary income only (standard deduction, HRA exemption, 80C, 80CCD(1B), 80D incl. senior parents,
home-loan interest on self-occupied property, 87A rebate, surcharge with marginal relief, 4% cess).
Use ONLY the passages provided. Cite the clause and passage_id for every step.
"""


def _fy_tag(fy: str) -> str:
    return fy.replace("-", "_")


def passages(fy: str) -> dict[str, Any]:
    return yaml.safe_load((PA_RULE_TEXTS / f"fy{_fy_tag(fy)}.yaml").read_text(encoding="utf-8"))


def build_prompt(fy: str) -> str:
    doc = passages(fy)
    body = "\n\n".join(f"[{p['passage_id']}] {p['clause']} (source: {p['source_id']})\n{p['text']}"
                       for p in doc["passages"])
    return f"{DSL_SPEC}\nFinancial year: {fy} (AY {doc['assessment_year']}).\n\nOFFICIAL PASSAGES:\n{body}\n"


def artifact_path(fy: str) -> Path:
    return PA_COMPILED / f"fy{_fy_tag(fy)}.compiled.json"


def sha256_of(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def compile_with_llm(fy: str) -> Path:
    """Call the LLM once and save a candidate artifact. Raises LLMUnavailable without a key."""
    prompt = build_prompt(fy)
    client = get_client(use_cache=False)
    resp = client.generate_json(prompt, system="You are a meticulous tax-law compiler. Output JSON only.")
    art = resp.parsed
    art["compiler"] = {"model": resp.model, "mode": resp.mode, "input_tokens": resp.input_tokens,
                       "output_tokens": resp.output_tokens, "cost_usd": resp.cost_usd,
                       "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                       "generated_at": dt.datetime.now().isoformat(timespec="seconds")}
    out = PA_COMPILED / "candidates" / f"fy{_fy_tag(fy)}_{dt.datetime.now():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(art, indent=1), encoding="utf-8")
    (PA_COMPILED / f"compile_prompt_fy{_fy_tag(fy)}.txt").write_text(prompt, encoding="utf-8")
    return out


def validate_artifact(art: dict[str, Any], fy: str) -> list[str]:
    """Static validation. Returns a list of problems (empty = valid)."""
    problems: list[str] = []
    if art.get("financial_year") != fy:
        problems.append(f"financial_year is {art.get('financial_year')!r}, expected {fy!r}")
    valid_passages = {p["passage_id"] for p in passages(fy)["passages"]}
    tables = art.get("tables", {})
    for name, rows in tables.items():
        for row in rows:
            if len(row) != 3:
                problems.append(f"table {name}: row {row} must be [lower, upper, rate]")
    for regime in ("old", "new"):
        steps = art.get("regimes", {}).get(regime, {}).get("steps", [])
        known = set(INPUT_FIELDS)
        for st in steps:
            sid = st.get("id", "?")
            for key in ("id", "output", "expr", "clause", "passage_id"):
                if not st.get(key):
                    problems.append(f"{regime}/{sid}: missing {key}")
            if st.get("passage_id") and st["passage_id"] not in valid_passages:
                problems.append(f"{regime}/{sid}: passage_id {st['passage_id']} not in official passages")
            try:
                unknown = rule_dsl.names_used(st.get("expr", "0")) - known
                if unknown:
                    problems.append(f"{regime}/{sid}: unknown names {sorted(unknown)}")
            except rule_dsl.DSLError as exc:
                problems.append(f"{regime}/{sid}: {exc}")
            known.add(st.get("output", ""))
        missing = [o for o in REQUIRED_OUTPUTS if o not in known]
        if missing:
            problems.append(f"{regime}: missing required outputs {missing}")
    return problems


def verify_against_suite(art: dict[str, Any], fy: str) -> dict[str, Any]:
    """Run the artifact on every test case of ``fy``; rupee-exact comparison of both regimes."""
    from project_a.llm_compiled_engine import run_artifact

    cases = [c for c in json.loads((PA_DATA / "tax_test_cases.json").read_text())["cases"]
             if c["input"]["financial_year"] == fy]
    failures = []
    for c in cases:
        try:
            res = run_artifact(art, TaxpayerProfile(**c["input"]))
            for reg in ("old", "new"):
                got = getattr(res, reg)
                exp = c["expected"][reg]
                if int(got.tax_payable) != exp["tax_payable"] or int(got.total_income) != exp["total_income"]:
                    failures.append({"case_id": c["case_id"], "regime": reg, "expected": exp["tax_payable"],
                                     "got": int(got.tax_payable)})
        except Exception as exc:  # noqa: BLE001
            failures.append({"case_id": c["case_id"], "error": str(exc)[:200]})
    return {"fy": fy, "cases": len(cases), "failed": len(failures), "passed": len(cases) - len({
        f["case_id"] for f in failures}), "failures": failures[:25]}


def approve(candidate: Path, fy: str, reviewer: str, notes: list[str]) -> Path:
    art = json.loads(candidate.read_text(encoding="utf-8"))
    problems = validate_artifact(art, fy)
    if problems:
        raise ValueError(f"cannot approve: validation problems: {problems[:5]}")
    ver = verify_against_suite(art, fy)
    if ver["failed"]:
        raise ValueError(f"cannot approve: {ver['failed']} test-suite failures, e.g. {ver['failures'][:3]}")
    art.pop("review", None)
    art["review"] = {"status": "approved", "reviewer": reviewer, "reviewed_on": dt.date.today().isoformat(),
                     "notes": notes, "verification": {k: ver[k] for k in ("cases", "passed", "failed")},
                     "artifact_sha256": sha256_of({k: v for k, v in art.items() if k != "review"})}
    out = artifact_path(fy)
    out.write_text(json.dumps(art, indent=1), encoding="utf-8")
    return out


def diff_artifacts(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Human-readable diff of two artifacts (tables and step expressions)."""
    out = []
    for t in sorted(set(a.get("tables", {})) | set(b.get("tables", {}))):
        if a.get("tables", {}).get(t) != b.get("tables", {}).get(t):
            out.append(f"table {t}: {a.get('tables', {}).get(t)} -> {b.get('tables', {}).get(t)}")
    for regime in ("old", "new"):
        sa = {s["id"]: s for s in a["regimes"][regime]["steps"]}
        sb = {s["id"]: s for s in b["regimes"][regime]["steps"]}
        for sid in sorted(set(sa) | set(sb)):
            ea, eb = sa.get(sid, {}).get("expr"), sb.get(sid, {}).get("expr")
            if ea != eb:
                out.append(f"{sid}: {ea!r} -> {eb!r}")
    return out
