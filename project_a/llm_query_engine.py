"""Engine B — LLM at query time: retrieve the relevant rule text, let the LLM compute, then verify.

Flow per request
----------------
1. **Retrieve** passages for the financial year (``rule_texts/fy*.yaml``) by topic: always slabs,
   standard deduction, 87A, cess, rounding and new-regime exclusions; HRA / 24(b) / 80C / 80CCD(1B) /
   80D only when the profile uses them; surcharge only when salary exceeds Rs 50 lakh.
   The corpus has ~13 passages, so deterministic topic retrieval is exact and auditable; embeddings
   would add cost and non-determinism without benefit at this size.
2. **Ask** Gemini for a JSON computation of both regimes, with each line citing ``passage_id``.
3. **Validate** (layers, all recorded in ``llm_meta.validation``):
   a. schema (Pydantic);
   b. internal arithmetic — total = tax − rebate + surcharge + cess, cess = 4%, payable = round10(total);
   c. citations — every line cites a passage that was actually retrieved;
   d. shadow check against Engine A (deterministic). Disagreement => ``status=REJECTED``.
4. The LLM's numbers are never silently replaced by Engine A's: a rejected answer is returned as
   rejected, with the diff, so callers (and the benchmark) can see exactly what the LLM got wrong.

Without ``GEMINI_API_KEY`` the engine returns ``status=UNAVAILABLE`` and no numbers.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

from common.llm_client import LLMResponseError, LLMUnavailable, get_client
from common.paths import PA_RULE_TEXTS
from project_a import rules_engine
from project_a.models import ComponentLine, EngineStatus, RegimeResult, TaxComputation, TaxpayerProfile

ENGINE_NAME = "llm_query_engine"
D = Decimal

SYSTEM = ("You are an Indian income-tax computation assistant. Use ONLY the official passages supplied. "
          "Compute exactly, in rupees, showing every component. Output JSON only.")


class LLMLine(BaseModel):
    name: str
    amount: float
    passage_id: str
    clause: str = ""


class LLMRegime(BaseModel):
    total_income: float
    tax_on_income: float
    rebate_87a: float = 0
    surcharge: float = 0
    cess: float = 0
    total_tax: float
    tax_payable: float
    lines: list[LLMLine] = Field(default_factory=list)


class LLMAnswer(BaseModel):
    old: LLMRegime
    new: LLMRegime
    recommended_regime: Literal["old", "new"]
    reasoning: str = ""


def _passages(fy: str) -> list[dict[str, Any]]:
    path = PA_RULE_TEXTS / f"fy{fy.replace('-', '_')}.yaml"
    if not path.exists():
        from project_a.rule_registry import RuleSetNotFound
        raise RuleSetNotFound(f"No rule text corpus for FY {fy}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))["passages"]


def retrieve(p: TaxpayerProfile) -> list[dict[str, Any]]:
    # Match on each passage's PRIMARY topic (topics[0]); secondary tags such as "new_regime" are descriptive only.
    wanted = {"slab", "standard_deduction", "rebate", "cess", "rounding", "new_regime"}
    if p.hra_received:
        wanted.add("hra")
    if p.home_loan_interest_self_occupied:
        wanted.add("home_loan")
    if p.section_80c:
        wanted.add("80c")
    if p.section_80ccd_1b:
        wanted.add("80ccd")
    if any((p.health_insurance_self_family, p.health_insurance_parents, p.preventive_checkup_self_family,
            p.preventive_checkup_parents, p.medical_expenditure_self_family, p.medical_expenditure_parents)):
        wanted.add("80d")
    if p.gross_salary > 5_000_000:
        wanted.add("surcharge")
    return [x for x in _passages(p.financial_year) if x["topics"][0] in wanted]


OUTPUT_SCHEMA = {
    "old": {"total_income": "number", "tax_on_income": "number", "rebate_87a": "number", "surcharge": "number",
            "cess": "number", "total_tax": "number", "tax_payable": "number",
            "lines": [{"name": "string", "amount": "number", "passage_id": "string", "clause": "string"}]},
    "new": "same shape as old",
    "recommended_regime": "old|new",
    "reasoning": "string (short)",
}


def build_prompt(p: TaxpayerProfile, passages: list[dict[str, Any]]) -> str:
    profile = p.model_dump()
    text = "\n\n".join(f"[{x['passage_id']}] {x['clause']}\n{x['text']}" for x in passages)
    return (f"Financial year {p.financial_year}. Taxpayer profile (annual rupees):\n{json.dumps(profile, indent=1)}\n\n"
            f"OFFICIAL PASSAGES:\n{text}\n\n"
            "Compute the income tax under BOTH the old regime and the new regime (Section 115BAC), salary income only. "
            "Round total income and tax payable to the nearest 10 rupees (Sections 288A/288B). "
            "Cite the passage_id for every line. Recommend the regime with the lower tax payable "
            "(if equal, the new regime).\n"
            f"Return JSON with exactly this shape:\n{json.dumps(OUTPUT_SCHEMA, indent=1)}")


def _arith_problems(r: LLMRegime) -> list[str]:
    probs = []
    if abs(r.tax_on_income - r.rebate_87a + r.surcharge + r.cess - r.total_tax) > 1:
        probs.append("total_tax != tax - rebate + surcharge + cess")
    if abs((r.tax_on_income - r.rebate_87a + r.surcharge) * 0.04 - r.cess) > 1:
        probs.append("cess != 4% of (tax - rebate + surcharge)")
    if int(rules_engine.round_to_10(D(str(max(r.total_tax, 0))))) != round(r.tax_payable):
        probs.append("tax_payable != round10(total_tax)")
    return probs


def _to_result(regime: str, r: LLMRegime, gross: int) -> RegimeResult:
    lines = [ComponentLine(name=l.name, amount=D(str(l.amount)), clause=l.clause or l.passage_id,
                           rule_id=l.passage_id, rule_version="llm-query") for l in r.lines]
    return RegimeResult(regime=regime, gross_salary=D(gross), total_income=D(str(r.total_income)),
                        tax_on_income=D(str(r.tax_on_income)), rebate_87a=D(str(r.rebate_87a)),
                        surcharge=D(str(r.surcharge)), cess=D(str(r.cess)), total_tax=D(str(r.total_tax)),
                        tax_payable=D(str(r.tax_payable)), lines=lines)


def compute(p: TaxpayerProfile, client: Any | None = None, use_cache: bool = True,
            shadow_verify: bool = True) -> TaxComputation:
    t0 = time.perf_counter()
    passages = retrieve(p)
    try:
        client = client or get_client(use_cache=use_cache)
    except LLMUnavailable as exc:
        return TaxComputation(engine=ENGINE_NAME, status=EngineStatus.UNAVAILABLE, financial_year=p.financial_year,
                              notes=[str(exc)], llm_meta={"mode": "unavailable"})
    prompt = build_prompt(p, passages)
    meta: dict[str, Any] = {"retrieved_passages": [x["passage_id"] for x in passages]}
    try:
        resp = client.generate_json(prompt, system=SYSTEM, use_cache=use_cache)
        meta.update(resp.summary())
        ans = LLMAnswer.model_validate(resp.parsed)
    except (LLMResponseError, ValidationError) as exc:
        meta["error"] = str(exc)[:500]
        return TaxComputation(engine=ENGINE_NAME, status=EngineStatus.ERROR, financial_year=p.financial_year,
                              notes=["LLM output unusable: " + str(exc)[:200]], llm_meta=meta)

    validation: dict[str, Any] = {}
    retrieved = {x["passage_id"] for x in passages}
    for reg in ("old", "new"):
        r: LLMRegime = getattr(ans, reg)
        validation[f"{reg}_arithmetic"] = _arith_problems(r)
        validation[f"{reg}_bad_citations"] = sorted({l.passage_id for l in r.lines} - retrieved)
    old_r, new_r = _to_result("old", ans.old, p.gross_salary), _to_result("new", ans.new, p.gross_salary)
    result = TaxComputation(engine=ENGINE_NAME, status=EngineStatus.OK, financial_year=p.financial_year,
                            rule_set_id=f"LLM-QUERY-FY{p.financial_year}", rule_set_version=resp.model,
                            old=old_r, new=new_r, recommended_regime=ans.recommended_regime,
                            savings=abs(old_r.tax_payable - new_r.tax_payable), notes=[ans.reasoning[:500]])
    if any(validation.values()):
        result.status = EngineStatus.UNVERIFIED
    if shadow_verify:
        truth = rules_engine.compute(p)
        diffs = {}
        for reg in ("old", "new"):
            exp, got = getattr(truth, reg), getattr(result, reg)
            for k in ("total_income", "tax_payable"):
                if int(getattr(exp, k)) != int(round(getattr(got, k))):
                    diffs[f"{reg}.{k}"] = {"llm": str(getattr(got, k)), "rules_engine": str(getattr(exp, k))}
        if truth.recommended_regime != result.recommended_regime:
            diffs["recommended_regime"] = {"llm": result.recommended_regime, "rules_engine": truth.recommended_regime}
        validation["shadow_diffs"] = diffs
        if diffs:
            result.status = EngineStatus.REJECTED
            result.notes.append("REJECTED: LLM answer disagrees with the deterministic engine; not shown to user.")
    meta["validation"] = validation
    meta["total_latency_ms"] = (time.perf_counter() - t0) * 1000
    result.llm_meta = meta
    return result
