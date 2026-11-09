"""Dedicated FY 2025-26 boundary tests: deductions, HRA and age (plus cross-year consistency).

Every expected value below is derived by hand from the statute in the comment next to it, independently of the
engines and of the reference oracle. The same cases are generated for FY 2024-25 (`BND-202425-X-*`), which makes
the cross-year check possible: the old regime did not change between the two years.

Age assumption (models.age_band, documented in RESULTS_A): `age` is the age reached at any time during the
previous year (i.e. by 31 March), so a person turning 60 during the year is a senior citizen for the whole year.
"""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from project_a import llm_compiled_engine, rules_engine
from project_a.models import TaxpayerProfile

from .conftest import candidate_artifact, cases_by

FY = "2025-26"
X = {c["case_id"].split("-X-", 1)[1]: c for c in cases_by("BND-202526-X-")}
X24 = {c["case_id"].split("-X-", 1)[1]: c for c in cases_by("BND-202425-X-")}


def _engines(case):
    p = TaxpayerProfile(**case["input"])
    yield "rules", rules_engine.compute(p)
    yield "candidate", llm_compiled_engine.run_artifact(candidate_artifact(p.financial_year), p)
    if "-80D-PP-" not in case["case_id"]:
        yield "approved", llm_compiled_engine.compute(p)


def _line(result, rule_id: str) -> Decimal:
    return sum((abs(ln.amount) for ln in result.old.lines if ln.rule_id == rule_id), Decimal(0))


# ---------------- A. deduction boundaries (old regime) ----------------------------------------------------------
DEDUCTION_LINE = {  # case suffix -> (rule id, hand value): deduction = min(claimed, statutory cap)
    "CAP-section_80c-150000-10": ("OLD.80C", 149990), "CAP-section_80c-150000+0": ("OLD.80C", 150000),
    "CAP-section_80c-150000+10": ("OLD.80C", 150000),
    "CAP-section_80ccd_1b-50000-10": ("OLD.80CCD1B", 49990), "CAP-section_80ccd_1b-50000+0": ("OLD.80CCD1B", 50000),
    "CAP-section_80ccd_1b-50000+10": ("OLD.80CCD1B", 50000),
    "CAP-home_loan_interest_self_occupied-200000-10": ("OLD.24B", 199990),
    "CAP-home_loan_interest_self_occupied-200000+0": ("OLD.24B", 200000),
    "CAP-home_loan_interest_self_occupied-200000+10": ("OLD.24B", 200000),
    "CAP-health_insurance_self_family-25000-10": ("OLD.80D", 24990),       # non-senior group: 25,000 cap
    "CAP-health_insurance_self_family-25000+0": ("OLD.80D", 25000),
    "CAP-health_insurance_self_family-25000+10": ("OLD.80D", 25000),
    "CAP-health_insurance_parents-25000-10": ("OLD.80D", 24990),
    "CAP-health_insurance_parents-25000+10": ("OLD.80D", 25000),
    "CAP-health_insurance_self_family-50000-age+10": ("OLD.80D", 50000),   # assessee 62: senior cap 50,000
    "CAP-health_insurance_self_family-50000-age-10": ("OLD.80D", 49990),
    "CAP-health_insurance_self_family-50000-spouse_is_senior+10": ("OLD.80D", 50000),
    "CAP-health_insurance_parents-50000-parents_are_senior-10": ("OLD.80D", 49990),
    "CAP-health_insurance_parents-50000-parents_are_senior+10": ("OLD.80D", 50000),
    "PREV-SELF-10": ("OLD.80D", 4990), "PREV-SELF+0": ("OLD.80D", 5000), "PREV-SELF+10": ("OLD.80D", 5000),
    "PREV-SPLIT-10": ("OLD.80D", 4990), "PREV-SPLIT+0": ("OLD.80D", 5000),  # 3,000 + 1,990 / 2,000
    "PREV-SPLIT+10": ("OLD.80D", 5000),                                      # 3,000 + 2,010 -> 5,000 aggregate
    "PREV-INSIDE-CAP-10": ("OLD.80D", 25000),  # 20,990 + min(5,000, 25,000-20,990=4,010)
    "PREV-INSIDE-CAP+0": ("OLD.80D", 25000), "PREV-INSIDE-CAP+10": ("OLD.80D", 25000),
}


@pytest.mark.parametrize("suffix", list(DEDUCTION_LINE))
def test_deduction_caps_fy2025_26(suffix):
    rule, hand = DEDUCTION_LINE[suffix]
    for name, r in _engines(X[suffix]):
        assert _line(r, rule) == hand, (name, suffix)


@pytest.mark.parametrize("suffix, total_income", [
    ("VIA-GTI-10", 0),          # GTI 2,24,990 < deductions 2,25,000 -> VIA limited to GTI (s.80A(2)) -> 0
    ("VIA-GTI+0", 0),           # GTI 2,25,000 = deductions -> 0
    ("VIA-GTI+10", 10),         # GTI 2,25,010 - 2,25,000 = 10
    ("VIA-AFTER-HP-LOSS", 0),   # 3,00,000 - 50,000 - 2,00,000 = 50,000 GTI; 80C limited to 50,000
    ("ALL-CAPS-EXCEEDED", 1975000),  # 25,00,000-50,000-2,00,000 = 22,50,000; VIA 1,50,000+50,000+25,000+50,000
])
def test_deductions_constrained_by_gti_fy2025_26(suffix, total_income):
    for name, r in _engines(X[suffix]):
        assert r.old.total_income == total_income, (name, suffix)


# ---------------- B. HRA limbs: least of (a) HRA received, (b) rent - 10% salary, (c) 50%/40% salary ----------
HRA = {
    "HRA-A-smallest": 200000,          # a 2,00,000; b 5,00,000-60,000=4,40,000; c 3,00,000
    "HRA-B-smallest": 90000,           # a 3,00,000; b 1,50,000-60,000=90,000; c 3,00,000
    "HRA-C-metro-smallest": 200000,    # salary 4,00,000: a 3,00,000; b 4,60,000; c 50% = 2,00,000
    "HRA-C-nonmetro-smallest": 160000,  # c 40% = 1,60,000
    "HRA-no-rent": 0, "HRA-no-hra-received": 0,
    "HRA-rent-below-10pct": 0,         # b = 50,000-60,000 < 0 -> 0
    "HRA-rent-exactly-10pct": 0,       # b = 0
    "HRA-rent-10pct-minus-10": 0,      # b = -10 -> 0
    "HRA-rent-10pct-plus-10": 10,      # b = 10
    "HRA-A-vs-B-10": 199990, "HRA-A-vs-B+0": 200000, "HRA-A-vs-B+10": 200000,          # b = 2,00,000
    "HRA-B-vs-Cmetro-10": 299990, "HRA-B-vs-Cmetro+0": 300000, "HRA-B-vs-Cmetro+10": 300000,  # c = 3,00,000
    "HRA-A-vs-Cnonmetro-10": 239990, "HRA-A-vs-Cnonmetro+0": 240000, "HRA-A-vs-Cnonmetro+10": 240000,  # c 2,40,000
}


@pytest.mark.parametrize("suffix", list(HRA))
def test_hra_limbs_fy2025_26(suffix):
    for name, r in _engines(X[suffix]):
        assert _line(r, "OLD.HRA") == HRA[suffix], (name, suffix)
        assert r.new.total_income == r.new.gross_salary - 75000  # no HRA exemption in the new regime


# ---------------- C. age bands (old regime slab table; senior tables for residents only) -----------------------
# gross 8,60,000 -> TI 8,10,000. below 60: 12,500 + 62,000 = 74,500; senior: 10,000 + 62,000 = 72,000;
# super senior: 62,000. gross 3,60,000 -> TI 3,10,000: below 60: 3,000; senior: 500; super senior: 0.
AGE = {(59, True): (74500, 3000), (60, True): (72000, 500), (79, True): (72000, 500), (80, True): (62000, 0),
       (59, False): (74500, 3000), (60, False): (74500, 3000), (79, False): (74500, 3000), (80, False): (74500, 3000)}


@pytest.mark.parametrize("age, resident", list(AGE))
def test_age_band_slab_selection_fy2025_26(age, resident):
    hi, lo = AGE[(age, resident)]
    tag = "RES" if resident else "NONRES"
    for name, r in _engines(X[f"AGE-{age}-{tag}-860000"]):
        assert r.old.tax_on_income == hi, (name, age, resident)
    for name, r in _engines(X[f"AGE-{age}-{tag}-360000"]):
        assert r.old.tax_on_income == lo, (name, age, resident)
        # 87A (TI 3,10,000 <= 5,00,000): residents pay 0; non-residents pay tax + 4% cess = 3,120
        assert r.old.tax_payable == (0 if resident else 3120), (name, age, resident)


@pytest.mark.parametrize("age", [17, 121])
def test_age_outside_specification_is_rejected(age):
    with pytest.raises(ValidationError):
        TaxpayerProfile(financial_year=FY, age=age, gross_salary=500000)


# ---------------- D. cross-year consistency ------------------------------------------------------------------
def test_dedicated_fy2025_26_suite_exists():
    cats = {c["category"] for c in X.values()}
    assert {"boundary_deduction_cap", "boundary_hra", "boundary_age", "boundary_80d_per_person"} <= cats
    assert len(X) == len(X24)


@pytest.mark.parametrize("suffix", sorted(X))
def test_old_regime_identical_across_years(suffix):
    """The old regime did not change: the same profile gives the same old-regime figures in both years."""
    a = rules_engine.compute(TaxpayerProfile(**X24[suffix]["input"])).old.key_figures()
    b = rules_engine.compute(TaxpayerProfile(**X[suffix]["input"])).old.key_figures()
    assert a == b
    assert X24[suffix]["expected"]["old"] == X[suffix]["expected"]["old"]  # the oracle agrees


def test_only_new_regime_differs_across_years():
    differ = [s for s in X if X24[s]["expected"]["new"] != X[s]["expected"]["new"]]
    assert differ, "the new regime changed in FY 2025-26, so some new-regime results must differ"
    for s in X:
        assert X24[s]["expected"]["old"] == X[s]["expected"]["old"]
