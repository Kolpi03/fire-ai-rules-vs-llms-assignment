"""Section 80D, first proviso, judged per person (human review 2026-11-09).

Statute (rule_texts P24-80D / P25-80D): medical expenditure under clauses (c)/(d) counts only if it is paid
"in respect of a senior citizen and no amount has been paid to effect or to keep in force an insurance on the
health of such person"; (a)+(c) and (b)+(d) are each capped at Rs 50,000 (second proviso); preventive
check-ups are inside those caps and limited to Rs 5,000 in aggregate (2A).

Expected 80D amounts (``hand_80d``) are derived by hand in each case's description, independently of the
engines and of the reference oracle; all three are checked against them.
"""
import copy
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from project_a import llm_compiled_engine, rules_engine
from project_a.models import TaxpayerProfile
from project_a.reference_oracle import deduction_80d
from project_a.rule_compiler import verify_against_suite

from .conftest import ROOT, candidate_artifact, cases_by

# The approved FY 2025-26 artifact as it was BEFORE the 80D fixes (commit f9c0bf7): it never reads the per-person
# inputs, so it is the right fixture for the "refuse, never ignore" guard of the compiled engine.
PRE_FIX = json.loads((ROOT / "project_a/tests/fixtures/fy2025_26_pre_80d_fix_approved.json").read_text())

PP = [c for c in cases_by("boundary_80d_per_person") if "hand_80d" in c]
NO_80D = {"health_insurance_self_family", "preventive_checkup_self_family", "medical_expenditure_self_family",
          "spouse_is_senior", "health_insurance_parents", "preventive_checkup_parents", "medical_expenditure_parents",
          "parents_are_senior", "self_insured", "spouse_insured", "medical_expenditure_self",
          "medical_expenditure_spouse", "parent_a_is_senior", "parent_a_insured", "medical_expenditure_parent_a",
          "parent_b_is_senior", "parent_b_insured", "medical_expenditure_parent_b"}


def _line(result, rule_id: str) -> Decimal:
    return sum((-ln.amount if ln.amount < 0 else ln.amount for ln in result.old.lines if ln.rule_id == rule_id),
               Decimal(0))


def test_cases_cover_both_years_and_all_mandatory_situations():
    ids = {c["case_id"].split("-80D-PP-")[1] for c in PP}
    for must in ("CHILD-INS-SPOUSE-SENIOR-UNINS-40000", "SENIOR-INS-NONSENIOR-SPOUSE-UNINS",
                 "SENIOR-UNINS-NO-PREMIUM-50000", "MULTI-SENIORS-MIXED", "PARENT-SENIOR-UNINSURED",
                 "PARENT-SENIOR-INSURED", "NONSENIOR-OWN-MEDICAL", "NONSENIOR-PARENT-MEDICAL", "COMBINED-ALL"):
        assert must in ids
    assert {c["input"]["financial_year"] for c in PP} == {"2024-25", "2025-26"}


@pytest.mark.parametrize("case", PP, ids=[c["case_id"] for c in PP])
def test_80d_matches_hand_derivation(case):
    p = TaxpayerProfile(**case["input"])
    hand = Decimal(case["hand_80d"])
    assert deduction_80d(case["input"]) == hand, "oracle"
    assert _line(rules_engine.compute(p), "OLD.80D") == hand, "Engine A"
    art = candidate_artifact(p.financial_year)
    assert art is not None
    assert _line(llm_compiled_engine.run_artifact(art, p), "OLD.80D") == hand, "candidate artifact"


@pytest.mark.parametrize("case", PP, ids=[c["case_id"] for c in PP])
def test_new_regime_is_not_affected_by_any_80d_input(case):
    with_80d = rules_engine.compute(TaxpayerProfile(**case["input"])).new
    without = rules_engine.compute(TaxpayerProfile(**{k: v for k, v in case["input"].items() if k not in NO_80D})).new
    assert with_80d.key_figures() == without.key_figures()


def test_before_after_mixed_insurance_example():
    """Insured child + uninsured senior spouse with Rs 40,000 of medical bills, premium Rs 20,000."""
    per_person = dict(financial_year="2025-26", age=45, gross_salary=1800000, spouse_is_senior=True,
                      health_insurance_self_family=20000, self_insured=False, spouse_insured=False,
                      medical_expenditure_spouse=40000)
    group_only = dict(financial_year="2025-26", age=45, gross_salary=1800000, spouse_is_senior=True,
                      health_insurance_self_family=20000, medical_expenditure_self_family=40000)
    # group representation cannot say who is insured -> conservative rule: premium only
    assert _line(rules_engine.compute(TaxpayerProfile(**group_only)), "OLD.80D") == 20000
    # per person: the spouse is a senior with no insurance -> 20,000 + 40,000, capped at 50,000
    assert _line(rules_engine.compute(TaxpayerProfile(**per_person)), "OLD.80D") == 50000
    # an artifact compiled before the fix refuses rather than silently ignoring the per-person fields
    with pytest.raises(llm_compiled_engine.UnsupportedInput):
        llm_compiled_engine.run_artifact(PRE_FIX, TaxpayerProfile(**per_person))
    # the approved artifact (the human-approved candidate) applies the fix
    assert _line(llm_compiled_engine.compute(TaxpayerProfile(**per_person)), "OLD.80D") == 50000


@pytest.mark.parametrize("bad, why", [
    (dict(age=70, health_insurance_self_family=10000, medical_expenditure_self=30000), "self_insured"),
    (dict(age=70, medical_expenditure_self_family=10000, medical_expenditure_self=30000), "not both"),
    (dict(age=70, self_insured=True, medical_expenditure_self=30000), "no health-insurance premium"),
    (dict(parent_a_is_senior=True, medical_expenditure_parent_a=10000), "parents_are_senior"),
    (dict(parents_are_senior=True, medical_expenditure_parent_a=10000), "parent_a_is_senior"),
    (dict(parents_are_senior=True, parent_a_is_senior=True, health_insurance_parents=20000, parent_a_insured=False,
          parent_b_insured=False), "neither parent"),
    (dict(age=45, spouse_is_senior=True, health_insurance_self_family=20000, self_insured=False), "spouse_insured"),
    (dict(age=65, health_insurance_self_family=20000, spouse_insured=False), "self_insured"),
    (dict(parents_are_senior=True, health_insurance_parents=20000, parent_a_insured=True), "which parent"),
    (dict(parents_are_senior=True, parent_b_is_senior=True, health_insurance_parents=20000, parent_a_insured=True),
     "parent_b_insured"),
])
def test_ambiguous_or_double_counted_inputs_are_rejected(bad, why):
    with pytest.raises(ValidationError, match=why):
        TaxpayerProfile(financial_year="2025-26", gross_salary=1500000, **{"age": 35, **bad})


def test_existing_group_level_behaviour_is_unchanged(tax_cases):
    """Every case that does not use the per-person fields keeps its previous expected value (all pass on Engine A)."""
    for c in tax_cases:
        if "-80D-PP-" in c["case_id"]:
            continue
        r = rules_engine.compute(TaxpayerProfile(**c["input"]))
        assert int(r.old.tax_payable) == c["expected"]["old"]["tax_payable"], c["case_id"]


# ---- mutation tests on the candidate: plausible mistakes in the corrected steps must fail the suite ----------
MUTANTS = {
    "group-level rule restored (the original bug)": {
        "OLD.80D.MED_A": "medical_expenditure_self_family + medical_expenditure_self + medical_expenditure_spouse "
                         "if (self_family_senior and health_insurance_self_family == 0) else 0"},
    "spouse insurance ignored": {
        "OLD.80D.MED_A": "(medical_expenditure_self if (age >= 60 and not self_insured) else 0) + "
                         "(medical_expenditure_spouse if spouse_is_senior else 0) + (medical_expenditure_self_family "
                         "if (self_family_senior and health_insurance_self_family == 0) else 0)"},
    "spouse seniority ignored": {
        "OLD.80D.MED_A": "(medical_expenditure_self if (age >= 60 and not self_insured) else 0) + "
                         "(medical_expenditure_spouse if not spouse_insured else 0) + (medical_expenditure_self_family "
                         "if (self_family_senior and health_insurance_self_family == 0) else 0)"},
    "spouse medical counted twice": {
        "OLD.80D.MED_A": "(medical_expenditure_self if (age >= 60 and not self_insured) else 0) + 2 * "
                         "(medical_expenditure_spouse if (spouse_is_senior and not spouse_insured) else 0) + "
                         "(medical_expenditure_self_family if (self_family_senior and health_insurance_self_family "
                         "== 0) else 0)"},
    "parent insurance ignored": {
        "OLD.80D.MED_B": "(medical_expenditure_parent_a if parent_a_is_senior else 0) + (medical_expenditure_parent_b "
                         "if parent_b_is_senior else 0) + (medical_expenditure_parents if (parents_are_senior and "
                         "health_insurance_parents == 0) else 0)"},
    "80D(4) limit raised by ANY senior in the group (the previous rule)": {
        "OLD.80D.CAP_A": "50000 if self_family_senior else 25000"},
    "parents' limit from parents_are_senior instead of an insured senior parent": {
        "OLD.80D.CAP_B": "50000 if parents_are_senior else 25000"},
    "second proviso dropped ((a)+(c) not capped at 50,000)": {"OLD.80D.MEDDED_A": "med_self_allowed"},
    "preventive check-up outside the clause (a) limit": {
        "OLD.80D.ROOM_A": "max(0, 50000 - prem_80d_self - med_80d_self)"},
    "preventive 5,000 not shared between the groups": {
        "OLD.80D.PREV_B": "min(preventive_checkup_parents, room_80d_parents, 5000)"},
    "group-level path dropped": {
        "OLD.80D.MED_A": "(medical_expenditure_self if (age >= 60 and not self_insured) else 0) + "
                         "(medical_expenditure_spouse if (spouse_is_senior and not spouse_insured) else 0)"},
}


@pytest.mark.parametrize("fy", ["2024-25", "2025-26"])
@pytest.mark.parametrize("name", list(MUTANTS))
def test_mutant_of_corrected_80d_is_caught(fy, name):
    art = copy.deepcopy(candidate_artifact(fy))
    for st in art["regimes"]["old"]["steps"]:
        if st["id"] in MUTANTS[name]:
            st["expr"] = MUTANTS[name][st["id"]]
    assert verify_against_suite(art, fy)["failed"] > 0, f"mutant survived: {name}"


def test_candidates_are_not_approved_and_fingerprints_match_manifest():
    from project_a.rule_compiler import sha256_of
    from .conftest import CANDIDATE_DIR
    manifest = json.loads((CANDIDATE_DIR / "80d_per_person_manifest.json").read_text())
    for fy, info in manifest["candidates"].items():
        art = candidate_artifact(fy)
        assert "review" not in art  # not approved, not signed
        assert sha256_of(art) == info["fingerprint_sha256"]
        assert info["static_validation_problems"] == [] and info["suite"]["failed"] == 0


def test_strict_80d4_scenarios_are_covered_in_both_years():
    ids = {c["case_id"].split("-80D-PP-")[1] for c in PP}
    for must in ("STRICT-CHILD-INS-SPOUSE-SENIOR-UNINS-PREM40000", "STRICT-INSURED-SENIOR-SPOUSE-PREM50000",
                 "STRICT-SPOUSE-UNINS-MED25000-CHILD-INS", "STRICT-PARENTS-MIXED", "STRICT-MULTI-INSURED-UNINSURED"):
        assert must in ids
    for limit in (25000, 50000):  # every limit tested 10 below, at and 10 above
        for d in (-10, 0, 10):
            assert any(f"PREM{limit + d}" in i for i in ids), (limit, d)
    years = {c["input"]["financial_year"] for c in PP if "STRICT" in c["case_id"]}
    assert years == {"2024-25", "2025-26"}


def test_uninsured_senior_does_not_raise_limit_for_insured_child():
    """The case the stricter reading changes: before, the group limit was 50,000 because a senior was present."""
    p = TaxpayerProfile(financial_year="2025-26", age=45, gross_salary=1800000, spouse_is_senior=True,
                        self_insured=False, spouse_insured=False, health_insurance_self_family=40000)
    assert _line(rules_engine.compute(p), "OLD.80D") == 25000
    assert deduction_80d(p.model_dump(exclude_unset=True)) == 25000
    assert _line(llm_compiled_engine.run_artifact(candidate_artifact("2025-26"), p), "OLD.80D") == 25000
    # group-only input of the same premium: who is insured is unknown -> the premium covers the senior spouse too
    g = TaxpayerProfile(financial_year="2025-26", age=45, gross_salary=1800000, spouse_is_senior=True,
                        health_insurance_self_family=40000)
    assert _line(rules_engine.compute(g), "OLD.80D") == 40000


def test_approved_artifact_refuses_stated_fields_even_with_default_values():
    """Regression: spouse_insured=False equals the default but switches 80D to per-person insurance; an artifact that
    never reads it must refuse, not answer with the group-level limit (found during the s.80D(4) change)."""
    p = TaxpayerProfile(financial_year="2025-26", age=45, gross_salary=1800000, spouse_is_senior=True,
                        self_insured=False, spouse_insured=False, health_insurance_self_family=40000)
    with pytest.raises(llm_compiled_engine.UnsupportedInput):
        llm_compiled_engine.run_artifact(PRE_FIX, p)
    legacy = TaxpayerProfile(financial_year="2025-26", age=45, gross_salary=1800000, spouse_is_senior=True,
                             health_insurance_self_family=40000)
    assert _line(llm_compiled_engine.run_artifact(PRE_FIX, legacy), "OLD.80D") == 40000  # group-only still answered
    # approved artifact (after human approval): strict s.80D(4) -> the insured child's premium is limited to 25,000
    assert _line(llm_compiled_engine.compute(p), "OLD.80D") == 25000
    assert _line(llm_compiled_engine.compute(legacy), "OLD.80D") == 40000


def test_approved_artifacts_are_the_human_approved_candidates():
    from project_a.rule_compiler import sha256_of
    from .conftest import CANDIDATE_DIR
    manifest = json.loads((CANDIDATE_DIR / "80d_per_person_manifest.json").read_text())
    for fy, info in manifest["candidates"].items():
        art = llm_compiled_engine.load_artifact(fy)
        assert art["review"]["status"] == "approved" and art["review"]["reviewer"] == "Kolpi03"
        assert art["review"]["artifact_sha256"] == info["fingerprint_sha256"]
        assert sha256_of({k: v for k, v in art.items() if k != "review"}) == info["fingerprint_sha256"]


def test_cli_rejects_ambiguous_input_with_a_question_not_a_traceback(capsys):
    from project_a.cli import main
    rc = main(["calc", "--engine", "rules", "--json", json.dumps(
        {"financial_year": "2025-26", "age": 70, "gross_salary": 1500000, "health_insurance_self_family": 10000,
         "medical_expenditure_self": 30000})])
    out = capsys.readouterr().out
    assert rc == 2 and "Input rejected" in out and "self_insured" in out and "Traceback" not in out
