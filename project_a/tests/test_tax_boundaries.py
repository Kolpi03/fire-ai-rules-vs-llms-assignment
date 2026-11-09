"""Every boundary case for both years on both deterministic engines, plus marginal-relief properties."""
from decimal import Decimal

import pytest

from project_a import llm_compiled_engine, rules_engine
from project_a.models import TaxpayerProfile

from .conftest import cases_by

BOUNDARY = cases_by("boundary")


@pytest.mark.parametrize("engine", [rules_engine.compute, llm_compiled_engine.compute], ids=["rules", "compiled"])
@pytest.mark.parametrize("case", BOUNDARY, ids=[c["case_id"] for c in BOUNDARY])
def test_boundary(engine, case):
    r = engine(TaxpayerProfile(**case["input"]))
    for reg in ("old", "new"):
        got, exp = getattr(r, reg), case["expected"][reg]
        assert int(got.total_income) == exp["total_income"]
        assert int(got.tax_payable) == exp["tax_payable"], f"{reg}: {got.key_figures()} vs {exp}"
        for k in ("tax_on_income", "rebate_87a", "surcharge", "cess"):
            assert abs(float(getattr(got, k)) - float(exp[k])) < 0.01


@pytest.mark.parametrize("fy,limit", [("2024-25", 700_000), ("2025-26", 1_200_000)])
def test_rebate_marginal_relief_property(fy, limit):
    """Above the 87A limit, tax before cess never exceeds the income above the limit (new regime)."""
    for ti in range(limit + 10, limit + 100_000, 970):
        p = TaxpayerProfile(financial_year=fy, age=30, gross_salary=ti + 75_000)
        r = rules_engine.compute(p).new
        assert r.tax_on_income - r.rebate_87a <= Decimal(int(r.total_income) - limit)


@pytest.mark.parametrize("threshold", [5_000_000, 10_000_000, 20_000_000])
@pytest.mark.parametrize("regime,std", [("old", 50_000), ("new", 75_000)])
def test_surcharge_marginal_relief_property(threshold, regime, std):
    """Just above a surcharge threshold, tax+surcharge exceeds the threshold liability by <= extra income."""
    at = getattr(rules_engine.compute(TaxpayerProfile(age=40, gross_salary=threshold + std)), regime)
    base = at.tax_on_income + at.surcharge
    for extra in (10, 1_000, 50_000, 150_000):
        r = getattr(rules_engine.compute(TaxpayerProfile(age=40, gross_salary=threshold + std + extra)), regime)
        assert r.tax_on_income + r.surcharge - base <= Decimal(extra) + Decimal("0.01")


@pytest.mark.parametrize("fy", ["2024-25", "2025-26"])
def test_tax_is_monotonic_in_salary(fy):
    prev = {"old": Decimal(0), "new": Decimal(0)}
    for g in range(0, 6_000_000, 7_310):
        r = rules_engine.compute(TaxpayerProfile(financial_year=fy, age=35, gross_salary=g))
        for reg in ("old", "new"):
            val = getattr(r, reg).total_tax
            assert val >= prev[reg] - Decimal("0.01"), (fy, reg, g)
            prev[reg] = val
