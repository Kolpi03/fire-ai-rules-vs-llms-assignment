"""Regression: versioned years coexist, determinism, fail-loud behaviour and the migration evidence."""
import random

import pytest

from project_a import llm_compiled_engine, rules_engine
from project_a.benchmark import fingerprint
from project_a.differential import random_profile
from project_a.law_change_migration import mutation_test, silent_failure_probe
from project_a.models import TaxpayerProfile
from project_a.rule_registry import available_years, get_rule_set


def test_both_years_registered_and_distinct():
    assert {"2024-25", "2025-26"} <= set(available_years())
    a, b = get_rule_set("2024-25"), get_rule_set("2025-26")
    assert a.id != b.id
    assert a.regime("old")["slabs"]["by_age_band"] == b.regime("old")["slabs"]["by_age_band"]  # old regime unchanged
    assert a.regime("new")["rebate_87a"]["income_limit"] == 700_000
    assert b.regime("new")["rebate_87a"]["income_limit"] == 1_200_000


@pytest.mark.parametrize("fy,salary,new_tax", [("2024-25", 1_275_000, 83_200), ("2025-26", 1_275_000, 0)])
def test_same_person_two_years(fy, salary, new_tax):
    r = rules_engine.compute(TaxpayerProfile(financial_year=fy, age=30, gross_salary=salary))
    assert int(r.new.tax_payable) == new_tax
    assert r.rule_set_id.endswith(fy.replace("-", "-"))


@pytest.mark.parametrize("engine", [rules_engine.compute, llm_compiled_engine.compute])
def test_determinism_ten_runs(engine):
    rng = random.Random(3)
    for _ in range(20):
        p = random_profile(rng, rng.choice(["2024-25", "2025-26"]))
        assert len({fingerprint(engine(p)) for _ in range(10)}) == 1


def test_differential_rules_vs_compiled():
    rng = random.Random(99)
    for _ in range(1500):
        p = random_profile(rng, rng.choice(["2024-25", "2025-26"]))
        a, c = rules_engine.compute(p), llm_compiled_engine.compute(p)
        assert a.old.key_figures() == c.old.key_figures() and a.new.key_figures() == c.new.key_figures()


def test_year_blind_answers_would_be_silently_wrong():
    res = silent_failure_probe()
    assert res["silently_wrong"] > 0.5 * res["n_cases"] and res["errors_raised"] == 0


def test_migration_mutants_are_caught_except_equivalent():
    res = mutation_test()
    for name, m in res["mutants"].items():
        assert m["caught"] or name.startswith("EQUIVALENT"), name
