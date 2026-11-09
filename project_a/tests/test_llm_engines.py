"""LLM engines: validation guards of the query-time engine (scripted MOCK client) and compiled-rule safety.

The mock client is used ONLY to exercise the guard rails (schema, arithmetic, citation and shadow checks).
No accuracy claim about Gemini is derived from these tests.
"""
import copy
import json

import pytest

from common.llm_client import MockLLMClient
from project_a import llm_compiled_engine, llm_query_engine, rule_dsl, rules_engine
from project_a.models import EngineStatus, TaxpayerProfile
from project_a.rule_compiler import artifact_path, validate_artifact

P = TaxpayerProfile(age=30, gross_salary=1_500_000, section_80c=150_000)


def _answer_from_truth(p, tamper=None):
    t = rules_engine.compute(p)

    def reg(r, rid):
        return {"total_income": float(r.total_income), "tax_on_income": float(r.tax_on_income),
                "rebate_87a": float(r.rebate_87a), "surcharge": float(r.surcharge), "cess": float(r.cess),
                "total_tax": float(r.total_tax), "tax_payable": float(r.tax_payable),
                "lines": [{"name": "Tax on income", "amount": float(r.tax_on_income), "passage_id": rid, "clause": "x"}]}
    ans = {"old": reg(t.old, "P24-OLD-SLABS"), "new": reg(t.new, "P24-NEW-SLABS"),
           "recommended_regime": t.recommended_regime, "reasoning": "mock"}
    if tamper:
        tamper(ans)
    return ans


def test_query_engine_accepts_consistent_answer():
    r = llm_query_engine.compute(P, client=MockLLMClient([_answer_from_truth(P)]))
    assert r.status == EngineStatus.OK
    assert r.llm_meta["mode"] == "mock"


def test_query_engine_rejects_wrong_answer_without_substituting():
    def wrong(a):
        a["new"]["tax_payable"] += 1000
        a["new"]["total_tax"] += 1000
        a["new"]["tax_on_income"] += 1000 / 1.04
        a["new"]["cess"] = (a["new"]["tax_on_income"] - a["new"]["rebate_87a"] + a["new"]["surcharge"]) * 0.04
    r = llm_query_engine.compute(P, client=MockLLMClient([_answer_from_truth(P, wrong)]))
    assert r.status == EngineStatus.REJECTED
    truth = rules_engine.compute(P)
    assert r.new.tax_payable != truth.new.tax_payable  # the LLM's number is kept, not silently replaced
    assert "new.tax_payable" in r.llm_meta["validation"]["shadow_diffs"]


def test_query_engine_flags_arithmetic_and_citations():
    def bad(a):
        a["old"]["cess"] += 50
        a["old"]["lines"][0]["passage_id"] = "P99-INVENTED"
    r = llm_query_engine.compute(P, client=MockLLMClient([_answer_from_truth(P, bad)]), shadow_verify=False)
    assert r.status == EngineStatus.UNVERIFIED
    v = r.llm_meta["validation"]
    assert v["old_arithmetic"] and v["old_bad_citations"] == ["P99-INVENTED"]


def test_query_engine_invalid_json_is_error():
    r = llm_query_engine.compute(P, client=MockLLMClient(["this is not json"]))
    assert r.status == EngineStatus.ERROR and r.old is None


def test_query_engine_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    r = llm_query_engine.compute(P)
    assert r.status == EngineStatus.UNAVAILABLE and r.old is None and r.new is None


def test_retrieval_is_profile_specific():
    ids = {p["passage_id"] for p in llm_query_engine.retrieve(P)}
    assert "P24-80C" in ids and "P24-HRA" not in ids and "P24-SURCHARGE" not in ids


@pytest.mark.parametrize("expr", ["__import__('os')", "x.y", "[1][0]", "(lambda: 1)()", "open('f')",
                                  "min(1, key=2)"])
def test_dsl_rejects_unsafe_syntax(expr):
    with pytest.raises(rule_dsl.DSLError):
        rule_dsl.parse(expr)


def test_dsl_arithmetic_is_decimal_exact():
    assert rule_dsl.evaluate("0.1 + 0.2", {}, {}) == rule_dsl.D("0.3")


def test_compiled_engine_refuses_unapproved(tmp_path, monkeypatch):
    art = json.loads(artifact_path("2024-25").read_text())
    art["review"]["status"] = "pending"
    fake = tmp_path / "fy2024_25.compiled.json"
    fake.write_text(json.dumps(art))
    monkeypatch.setattr(llm_compiled_engine, "artifact_path", lambda fy: fake)
    llm_compiled_engine.load_artifact.cache_clear()
    with pytest.raises(llm_compiled_engine.UnapprovedArtifact):
        llm_compiled_engine.load_artifact("2024-25")
    llm_compiled_engine.load_artifact.cache_clear()


def test_validator_catches_unknown_names_and_missing_outputs():
    art = copy.deepcopy(json.loads(artifact_path("2024-25").read_text()))
    art["regimes"]["new"]["steps"][0]["expr"] = "min(75000, grosss_salary)"
    art["regimes"]["old"]["steps"] = [s for s in art["regimes"]["old"]["steps"] if s["output"] != "cess"]
    problems = validate_artifact(art, "2024-25")
    assert any("grosss_salary" in p for p in problems)
    assert any("missing required outputs" in p for p in problems)


def test_compiled_matches_rules_on_suite(tax_cases):
    """Approved artifact: identical to Engine A on every case it answers; it must refuse every case that STATES a
    field it never reads (even with a default-looking value such as spouse_insured=False). Never a wrong answer."""
    from .conftest import run_approved_or_refusal
    refused = 0
    for c in tax_cases:
        p = TaxpayerProfile(**c["input"])
        a, b = rules_engine.compute(p), run_approved_or_refusal(p)
        if b == "refused":
            refused += 1
            continue
        assert a.old.key_figures() == b.old.key_figures() and a.new.key_figures() == b.new.key_figures(), c["case_id"]
    # refusals are allowed only for cases that state a field the approved artifact never reads
    expected_refusals = 0
    for c in tax_cases:
        used = llm_compiled_engine.referenced_names(llm_compiled_engine.load_artifact(c["input"]["financial_year"]))
        expected_refusals += any(k not in used for k in c["input"] if k != "financial_year")
    assert refused == expected_refusals


def test_candidate_matches_rules_on_full_suite(tax_cases):
    """80D per-person candidates: identical to Engine A on EVERY case of their year, no refusals."""
    from .conftest import candidate_artifact
    for c in tax_cases:
        p = TaxpayerProfile(**c["input"])
        art = candidate_artifact(p.financial_year)
        if art is None:
            pytest.skip("no candidate")
        a, b = rules_engine.compute(p), llm_compiled_engine.run_artifact(art, p)
        assert a.old.key_figures() == b.old.key_figures() and a.new.key_figures() == b.new.key_figures(), c["case_id"]
