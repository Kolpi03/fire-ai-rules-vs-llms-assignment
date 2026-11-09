"""Engine A (ground truth) against hand-worked cases, the full suite and its invariants."""
from decimal import Decimal

import pytest

from project_a import reference_oracle, rules_engine
from project_a.models import TaxpayerProfile
from project_a.rule_registry import RuleSetNotFound, get_rule_set
from project_a.test_case_generator import HAND_WORKED

from .conftest import cases_by


@pytest.mark.parametrize("hw", HAND_WORKED, ids=[h["case_id"] for h in HAND_WORKED])
def test_hand_worked_cases(hw):
    """Numbers written out by hand in the derivation must match both the engine and the oracle."""
    full = {"financial_year": "2024-25", "age": 30, **hw["input"]}
    res = rules_engine.compute(TaxpayerProfile(**full))
    oracle = reference_oracle.expected(full)
    for regime, value in hw["hand"].items():
        assert int(getattr(res, regime).tax_payable) == value, hw["derivation"]
        assert oracle[regime]["tax_payable"] == value


def test_full_suite_rupee_exact(tax_cases):
    failures = []
    for c in tax_cases:
        r = rules_engine.compute(TaxpayerProfile(**c["input"]))
        for reg in ("old", "new"):
            got, exp = getattr(r, reg), c["expected"][reg]
            if int(got.tax_payable) != exp["tax_payable"] or int(got.total_income) != exp["total_income"]:
                failures.append((c["case_id"], reg))
        if r.recommended_regime != c["expected"]["recommended_regime"]:
            failures.append((c["case_id"], "recommendation"))
    assert not failures, failures[:10]
    assert len(tax_cases) >= 200


@pytest.mark.parametrize("value,expected", [(0, 0), (4, 0), (5, 10), (14, 10), (15, 20), (1234.99, 1230),
                                            (1235, 1240), (99995, 100000)])
def test_round_288(value, expected):
    assert rules_engine.round_to_10(Decimal(str(value))) == expected


def test_every_line_cites_a_known_rule():
    p = TaxpayerProfile(age=40, gross_salary=6_000_000, basic_plus_da=2_000_000, hra_received=600_000,
                        annual_rent_paid=900_000, metro_city=True, section_80c=150_000, section_80ccd_1b=50_000,
                        health_insurance_parents=40_000, parents_are_senior=True,
                        home_loan_interest_self_occupied=300_000)
    r = rules_engine.compute(p)
    ids = get_rule_set("2024-25").rule_ids() | {"INPUT.SALARY", "OLD.80A2"}
    for reg in (r.old, r.new):
        for line in reg.lines:
            assert line.clause and line.rule_id in ids and line.rule_version == "1.0.0"


def test_new_regime_ignores_old_regime_deductions():
    base = TaxpayerProfile(age=35, gross_salary=1_500_000)
    loaded = TaxpayerProfile(age=35, gross_salary=1_500_000, basic_plus_da=600_000, hra_received=200_000,
                             annual_rent_paid=400_000, section_80c=150_000, section_80ccd_1b=50_000,
                             health_insurance_self_family=25_000, home_loan_interest_self_occupied=200_000)
    assert rules_engine.compute(base).new.tax_payable == rules_engine.compute(loaded).new.tax_payable
    assert rules_engine.compute(loaded).old.tax_payable < rules_engine.compute(base).old.tax_payable


def test_non_resident_gets_no_rebate_or_senior_slab():
    res = rules_engine.compute(TaxpayerProfile(age=70, is_resident=False, gross_salary=550_000))
    assert res.old.rebate_87a == 0 and res.new.rebate_87a == 0
    resident = rules_engine.compute(TaxpayerProfile(age=70, gross_salary=550_000))
    assert resident.old.tax_payable == 0


def test_unknown_year_fails_loudly():
    with pytest.raises(RuleSetNotFound):
        get_rule_set("2026-27")


def test_tie_recommends_new_regime():
    r = rules_engine.compute(TaxpayerProfile(age=30, gross_salary=550_000))
    assert r.old.tax_payable == r.new.tax_payable == 0
    assert r.recommended_regime == "new" and r.savings == 0


@pytest.mark.parametrize("bad", [dict(hra_received=2_000_000, gross_salary=1_000_000, basic_plus_da=1),
                                 dict(gross_salary=-1), dict(age=10, gross_salary=100),
                                 dict(gross_salary=1_000_000, hra_received=100_000)])
def test_input_validation(bad):
    with pytest.raises(Exception):
        TaxpayerProfile(**{"age": 30, **bad})


def test_suite_categories_cover_every_threshold():
    cats = {c["category"] for c in cases_by("")}
    for needed in ("boundary_slab", "boundary_rebate", "boundary_surcharge", "boundary_deduction_cap",
                   "boundary_hra", "boundary_age", "boundary_rounding", "hand_worked", "realistic"):
        assert needed in cats
