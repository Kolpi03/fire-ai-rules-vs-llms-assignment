"""Random taxpayer profiles for differential testing (Engine A vs Engine C vs the reference oracle).

The generator deliberately over-samples edges: ages around 60/80, salaries around rebate and surcharge
thresholds, deductions above their caps, and senior/non-resident flags.
"""
from __future__ import annotations

import random

from project_a.models import TaxpayerProfile


def random_profile(rng: random.Random, fy: str) -> TaxpayerProfile:
    g = rng.choice([rng.randint(0, 2_000_000), rng.randint(0, 8_000_000), rng.randint(40_000_000, 600_000_000),
                    rng.randint(600_000, 1_500_000)])
    basic = rng.randint(0, g // 2)
    hra = rng.randint(0, min(g - basic, basic)) if basic else 0
    pick = lambda hi: rng.choice([0, rng.randint(0, hi)])  # noqa: E731
    return TaxpayerProfile(
        financial_year=fy, age=rng.choice([25, 59, 60, 79, 80, 85]), is_resident=rng.random() < .9, gross_salary=g,
        basic_plus_da=basic, hra_received=hra, annual_rent_paid=pick(900000), metro_city=rng.random() < .5,
        section_80c=pick(250000), section_80ccd_1b=pick(90000), health_insurance_self_family=pick(70000),
        preventive_checkup_self_family=pick(9000), medical_expenditure_self_family=pick(70000),
        spouse_is_senior=rng.random() < .2, health_insurance_parents=pick(70000), preventive_checkup_parents=pick(9000),
        medical_expenditure_parents=pick(70000), parents_are_senior=rng.random() < .5,
        home_loan_interest_self_occupied=pick(400000))
