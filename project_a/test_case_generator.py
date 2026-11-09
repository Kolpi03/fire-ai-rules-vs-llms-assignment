"""Generate the structured tax test suite (Task A1).

Three sources of expected values, in decreasing order of independence:

1. ``HAND_WORKED`` — cases whose arithmetic is written out by hand in ``derivation``; both the
   oracle and every engine are asserted against these numbers.
2. Boundary cases — for every threshold (slab edges for each age band and regime, 87A limits and
   the end of the marginal-relief zone, surcharge bands and their marginal-relief zones, every
   deduction cap, age 59/60/79/80, 288A rounding digits, salary below the standard deduction).
   Expected values come from :mod:`project_a.reference_oracle` (independent implementation).
3. Seeded realistic profiles — salary/deduction mixes typical of Indian salaried employees.

The 30-case official-calculator worksheet (``official_calculator_worksheet.csv``) is selected from
these cases; it ships with the official columns EMPTY and status PENDING until a human enters the
calculator's results (the calculator is a server-side web app and could not be called from here).
"""
from __future__ import annotations

import csv
import json
import random
from typing import Any

from common.paths import PA_DATA
from project_a.reference_oracle import FY_CONSTANTS, expected, slab_tax

FYS = ("2024-25", "2025-26")
STD = {"old": 50000, "new": 75000}

# --------------------------------------------------------------------------------------------
# 1. Hand-worked cases (arithmetic done by hand; see `derivation`)
# --------------------------------------------------------------------------------------------
HAND_WORKED: list[dict[str, Any]] = [
    {"case_id": "HW-01", "input": {"financial_year": "2024-25", "age": 30, "gross_salary": 775000},
     "hand": {"old": 59800, "new": 0},
     "derivation": "NEW: TI=7,75,000-75,000=7,00,000; tax=4L*5%=20,000; TI<=7L so 87A=20,000; payable 0. "
                   "OLD: TI=7,25,000; tax=12,500+2,25,000*20%=57,500; cess 2,300; payable 59,800."},
    {"case_id": "HW-02", "input": {"financial_year": "2024-25", "age": 30, "gross_salary": 785000},
     "hand": {"old": 61880, "new": 10400},
     "derivation": "NEW: TI=7,10,000; tax=20,000+10,000*10%=21,000; excess over 7L=10,000; 87A marginal "
                   "relief=21,000-10,000=11,000; net 10,000; cess 400; payable 10,400. OLD: TI=7,35,000; "
                   "tax=12,500+47,000=59,500; cess 2,380; payable 61,880."},
    {"case_id": "HW-03", "input": {"financial_year": "2024-25", "age": 30, "gross_salary": 797230},
     "hand": {"new": 23110},
     "derivation": "NEW: TI=7,22,230; tax=20,000+2,223=22,223 < excess 22,230 so no rebate; cess 888.92; "
                   "total 23,111.92; 288B -> 23,110."},
    {"case_id": "HW-04", "input": {"financial_year": "2024-25", "age": 30, "gross_salary": 550000},
     "hand": {"old": 0, "new": 0},
     "derivation": "OLD: TI=5,00,000; tax=12,500; 87A=12,500; 0. NEW: TI=4,75,000; tax=8,750; 87A full; 0. Tie -> new."},
    {"case_id": "HW-05", "input": {"financial_year": "2024-25", "age": 30, "gross_salary": 550010},
     "hand": {"old": 13000, "new": 0},
     "derivation": "OLD: TI=5,00,010 > 5L so no 87A; tax=12,500+2=12,502; cess 500.08; total 13,002.08 -> 13,000. "
                   "NEW: TI=4,75,010; tax 8,750.50; 87A full; 0."},
    {"case_id": "HW-06", "input": {"financial_year": "2024-25", "age": 65, "gross_salary": 1050000, "section_80c": 150000},
     "hand": {"old": 83200, "new": 49400},
     "derivation": "OLD senior: TI=10,50,000-50,000-1,50,000=8,50,000; tax=2L*5%+3.5L*20%=10,000+70,000=80,000; "
                   "cess 3,200; 83,200. NEW: TI=9,75,000; tax=20,000+27,500=47,500; cess 1,900; 49,400."},
    {"case_id": "HW-07", "input": {"financial_year": "2024-25", "age": 82, "gross_salary": 1050000},
     "hand": {"old": 104000, "new": 49400},
     "derivation": "OLD super-senior: TI=10,00,000; tax=5L*20%=1,00,000; cess 4,000; 1,04,000. NEW as HW-06: 49,400."},
    {"case_id": "HW-08", "input": {"financial_year": "2024-25", "age": 45, "gross_salary": 5085000},
     "hand": {"old": 1401400, "new": 1248000},
     "derivation": "NEW: TI=50,10,000; tax=20k+30k+30k+60k+35,10,000*30%=11,93,000; surcharge 10%=1,19,300; "
                   "tax at 50L=11,90,000; ceiling=11,90,000+10,000=12,00,000; relief=13,12,300-12,00,000=1,12,300; "
                   "surcharge=7,000; cess=12,00,000*4%=48,000; 12,48,000. OLD: TI=50,35,000; tax=1,12,500+40,35,000*30%"
                   "=13,23,000; surcharge 1,32,300; tax at 50L=13,12,500; ceiling=13,47,500; surcharge=24,500; "
                   "cess=53,900; 14,01,400."},
    {"case_id": "HW-09", "input": {"financial_year": "2024-25", "age": 35, "gross_salary": 1200000, "basic_plus_da": 600000,
                                   "hra_received": 240000, "annual_rent_paid": 420000, "metro_city": True},
     "hand": {"old": 98280, "new": 71500},
     "derivation": "HRA least of (a) 2,40,000 (b) 4,20,000-60,000=3,60,000 (c) 50%*6,00,000=3,00,000 = 2,40,000 (a). "
                   "OLD: TI=12L-2.4L-50k=9,10,000; tax=12,500+4,10,000*20%=94,500; cess 3,780; 98,280. "
                   "NEW: TI=11,25,000; tax=20k+30k+18,750=68,750; cess 2,750; 71,500."},
    {"case_id": "HW-10", "input": {"financial_year": "2024-25", "age": 35, "gross_salary": 1200000, "basic_plus_da": 600000,
                                   "hra_received": 300000, "annual_rent_paid": 200000, "metro_city": False},
     "hand": {"old": 120120},
     "derivation": "HRA least of (a) 3,00,000 (b) 2,00,000-60,000=1,40,000 (c) 40%*6L=2,40,000 = 1,40,000 (b). "
                   "OLD: TI=12L-1.4L-50k=10,10,000; tax=1,12,500+3,000=1,15,500; cess 4,620; 1,20,120."},
    {"case_id": "HW-11", "input": {"financial_year": "2024-25", "age": 35, "gross_salary": 1200000, "basic_plus_da": 500000,
                                   "hra_received": 300000, "annual_rent_paid": 400000, "metro_city": False},
     "hand": {"old": 106600},
     "derivation": "HRA least of (a) 3,00,000 (b) 4,00,000-50,000=3,50,000 (c) 40%*5L=2,00,000 = 2,00,000 (c). "
                   "OLD: TI=12L-2L-50k=9,50,000; tax=12,500+90,000=1,02,500; cess 4,100; 1,06,600."},
    {"case_id": "HW-12", "input": {"financial_year": "2024-25", "age": 40, "gross_salary": 1500000, "section_80c": 200000,
                                   "section_80ccd_1b": 60000, "health_insurance_self_family": 30000,
                                   "preventive_checkup_self_family": 3000, "medical_expenditure_parents": 60000,
                                   "preventive_checkup_parents": 4000, "parents_are_senior": True,
                                   "home_loan_interest_self_occupied": 250000},
     "hand": {"old": 111800, "new": 130000},
     "derivation": "80C min(2L,1.5L)=1.5L; 80CCD(1B) min(60k,50k)=50k; 80D self: min(30k,25k)=25k, no room for "
                   "preventive; parents senior & uninsured: medical min(60k,50k)=50k, no room for preventive; 80D=75k; "
                   "24(b) min(2.5L,2L)=2L. OLD: TI=15L-50k-2L-1.5L-50k-75k=9,75,000; tax=12,500+95,000=1,07,500; "
                   "cess 4,300; 1,11,800. NEW: TI=14,25,000; tax=1,25,000; cess 5,000; 1,30,000. Old saves 18,200."},
    {"case_id": "HW-13", "input": {"financial_year": "2024-25", "age": 65, "is_resident": False, "gross_salary": 550000},
     "hand": {"old": 13000, "new": 9100},
     "derivation": "Non-resident: no 87A, no senior slabs. OLD: TI=5,00,000; tax=12,500; cess 500; 13,000. "
                   "NEW: TI=4,75,000; tax=8,750; cess 350; 9,100."},
    {"case_id": "HW-14", "input": {"financial_year": "2025-26", "age": 30, "gross_salary": 1275000},
     "hand": {"old": 187200, "new": 0},
     "derivation": "FY25-26 NEW: TI=12,00,000; tax=4L*5%+4L*10%=60,000; 87A=60,000; 0. OLD: TI=12,25,000; "
                   "tax=1,12,500+67,500=1,80,000; cess 7,200; 1,87,200."},
    {"case_id": "HW-15", "input": {"financial_year": "2025-26", "age": 30, "gross_salary": 1325000},
     "hand": {"new": 52000},
     "derivation": "FY25-26 NEW: TI=12,50,000; tax=60,000+7,500=67,500; excess over 12L=50,000; 87A marginal "
                   "relief=17,500; net 50,000; cess 2,000; 52,000."},
    {"case_id": "HW-16", "input": {"financial_year": "2025-26", "age": 30, "gross_salary": 2575000},
     "hand": {"new": 343200},
     "derivation": "FY25-26 NEW: TI=25,00,000; tax=20k+40k+60k+80k+1,00,000+1L*30%=3,30,000; cess 13,200; 3,43,200."},
]


def _case(case_id: str, category: str, description: str, inp: dict, **extra: Any) -> dict:
    full = {"financial_year": "2024-25", "age": 30, **inp}
    return {"case_id": case_id, "category": category, "description": description, "input": full,
            "expected": expected(full), "expected_source": "reference_oracle", **extra}


def _gross_for_total_income(ti: int, regime: str) -> int:
    """Gross salary that yields a given total income with only the standard deduction."""
    return ti + STD[regime]


def boundary_cases() -> list[dict]:
    cases: list[dict] = []
    for fy in FYS:
        k = FY_CONSTANTS[fy]
        tag = fy.replace("-", "")
        # Old-regime slab edges per age band
        for band, age in (("below_60", 35), ("senior_60_79", 65), ("super_senior_80_plus", 85)):
            for thr, _ in k.old_slabs[band]:
                for d in (-10, 0, 10):
                    ti = thr + d
                    cases.append(_case(f"BND-{tag}-OLD-{band}-{thr}{d:+d}", "boundary_slab",
                                       f"Old regime {band} slab edge {thr:,} {d:+d}",
                                       {"financial_year": fy, "age": age,
                                        "gross_salary": _gross_for_total_income(ti, "old")},
                                       threshold=f"old slab {thr}"))
        # New-regime slab edges
        for thr, _ in k.new_slabs:
            for d in (-10, 0, 10):
                ti = thr + d
                cases.append(_case(f"BND-{tag}-NEW-{thr}{d:+d}", "boundary_slab",
                                   f"New regime slab edge {thr:,} {d:+d}",
                                   {"financial_year": fy, "gross_salary": _gross_for_total_income(ti, "new")},
                                   threshold=f"new slab {thr}"))
        # 87A: limit edges and end of the new-regime marginal-relief zone
        o_lim, _ = k.old_rebate
        for d in (-10, 0, 10, 20):
            cases.append(_case(f"BND-{tag}-87A-OLD{d:+d}", "boundary_rebate", f"Old 87A limit {o_lim:,} {d:+d}",
                               {"financial_year": fy, "gross_salary": _gross_for_total_income(o_lim + d, "old")},
                               threshold="87A old"))
        n_lim, _ = k.new_rebate
        # marginal relief ends where slab_tax(TI) == TI - limit; find first TI (multiple of 10) where it stops
        ti = n_lim
        while slab_tax(ti, k.new_slabs) > ti - n_lim:
            ti += 10
        mr_end = ti
        for ti_ in (n_lim - 10, n_lim, n_lim + 10, n_lim + 50000, mr_end - 20, mr_end - 10, mr_end, mr_end + 10):
            cases.append(_case(f"BND-{tag}-87A-NEW-{ti_}", "boundary_rebate",
                               f"New 87A limit {n_lim:,} / marginal relief end {mr_end:,}: TI {ti_:,}",
                               {"financial_year": fy, "gross_salary": _gross_for_total_income(ti_, "new")},
                               threshold="87A new"))
        # Non-resident just under rebate limit
        cases.append(_case(f"BND-{tag}-87A-NONRES", "boundary_rebate", "Non-resident gets no 87A rebate",
                           {"financial_year": fy, "is_resident": False,
                            "gross_salary": _gross_for_total_income(n_lim - 10, "new")}, threshold="87A residency"))
        # Surcharge bands (both regimes) and marginal-relief zones
        for thr in (5000000, 10000000, 20000000, 50000000):
            for d in (-10, 0, 10, 100000, 300000):
                for reg in ("old", "new"):
                    cases.append(_case(f"BND-{tag}-SC-{reg.upper()}-{thr}{d:+d}", "boundary_surcharge",
                                       f"Surcharge threshold {thr:,} {d:+d} (TI in {reg} regime)",
                                       {"financial_year": fy, "gross_salary": _gross_for_total_income(thr + d, reg)},
                                       threshold=f"surcharge {thr}"))
    # Deduction caps (FY-independent rules; run in FY 2024-25)
    base = {"age": 35, "gross_salary": 1800000}
    for fld, cap in (("section_80c", 150000), ("section_80ccd_1b", 50000), ("home_loan_interest_self_occupied", 200000),
                     ("health_insurance_self_family", 25000), ("health_insurance_parents", 25000)):
        for d in (-10, 0, 10):
            cases.append(_case(f"BND-CAP-{fld}-{cap}{d:+d}", "boundary_deduction_cap", f"{fld} at cap {cap:,} {d:+d}",
                               {**base, fld: cap + d}, threshold=f"cap {fld}"))
    for d in (-10, 0, 10):
        cases.append(_case(f"BND-CAP-80D-PARENTS-SENIOR{d:+d}", "boundary_deduction_cap", "80D senior parents cap 50,000",
                           {**base, "health_insurance_parents": 50000 + d, "parents_are_senior": True}, threshold="80D senior"))
        cases.append(_case(f"BND-CAP-80D-SELF-SENIOR{d:+d}", "boundary_deduction_cap", "80D senior self cap 50,000",
                           {**base, "age": 62, "health_insurance_self_family": 50000 + d}, threshold="80D senior"))
        cases.append(_case(f"BND-CAP-80D-PREVENTIVE{d:+d}", "boundary_deduction_cap", "80D preventive aggregate 5,000",
                           {**base, "preventive_checkup_self_family": 3000, "preventive_checkup_parents": 2000 + d},
                           threshold="80D preventive"))
    cases.append(_case("BND-80D-MEDICAL-INSURED-PARENT", "boundary_deduction_cap",
                       "Medical expenditure not allowed when parents are insured",
                       {**base, "parents_are_senior": True, "health_insurance_parents": 10000,
                        "medical_expenditure_parents": 40000}, threshold="80D medical"))
    cases.append(_case("BND-80D-MEDICAL-NONSENIOR", "boundary_deduction_cap",
                       "Medical expenditure not allowed for non-senior self",
                       {**base, "medical_expenditure_self_family": 30000}, threshold="80D medical"))
    cases.append(_case("BND-80D-SPOUSE-SENIOR", "boundary_deduction_cap", "Spouse senior raises self/family cap",
                       {**base, "spouse_is_senior": True, "health_insurance_self_family": 45000}, threshold="80D senior"))
    # HRA: each branch of the least-of rule, metro vs non-metro, no rent
    for name, h in (("A-min", dict(basic_plus_da=600000, hra_received=200000, annual_rent_paid=500000, metro_city=True)),
                    ("B-min", dict(basic_plus_da=600000, hra_received=300000, annual_rent_paid=150000, metro_city=True)),
                    ("C-min-metro", dict(basic_plus_da=400000, hra_received=300000, annual_rent_paid=500000, metro_city=True)),
                    ("C-min-nonmetro", dict(basic_plus_da=400000, hra_received=300000, annual_rent_paid=500000, metro_city=False)),
                    ("rent-below-10pct", dict(basic_plus_da=600000, hra_received=200000, annual_rent_paid=50000)),
                    ("no-rent", dict(basic_plus_da=600000, hra_received=200000, annual_rent_paid=0))):
        cases.append(_case(f"BND-HRA-{name}", "boundary_hra", f"HRA branch {name}",
                           {"age": 32, "gross_salary": 1400000, **h}, threshold="HRA"))
    # Age boundaries
    for age in (59, 60, 79, 80):
        cases.append(_case(f"BND-AGE-{age}", "boundary_age", f"Age {age}", {"age": age, "gross_salary": 860000},
                           threshold="age band"))
    # 288A rounding: total income last digit 4 vs 5
    for last in (4, 5, 6):
        cases.append(_case(f"BND-ROUND-288A-{last}", "boundary_rounding", f"Total income ending in {last}",
                           {"gross_salary": 1_250_000 + last}, threshold="288A"))
    # Salary below standard deduction; zero salary; deductions exceeding GTI
    cases.append(_case("BND-STD-BELOW", "boundary_std_deduction", "Salary below standard deduction", {"gross_salary": 40000},
                       threshold="16(ia)"))
    cases.append(_case("BND-ZERO", "boundary_std_deduction", "Zero salary", {"gross_salary": 0}, threshold="zero"))
    cases.append(_case("BND-VIA-EXCEEDS-GTI", "boundary_deduction_cap", "Deductions exceed gross total income",
                       {"gross_salary": 260000, "section_80c": 150000, "section_80ccd_1b": 50000,
                        "health_insurance_self_family": 25000}, threshold="80A(2)"))
    cases.append(_case("BND-HP-LOSS-EXCEEDS-SALARY", "boundary_deduction_cap", "House-property loss exceeds salary income",
                       {"gross_salary": 200000, "home_loan_interest_self_occupied": 200000}, threshold="71(3A)"))
    return cases


def realistic_cases(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    cities_metro = [True, False]
    out = []
    for i in range(n):
        fy = FYS[i % 2]
        gross = rng.choice([rng.randrange(300000, 1500000, 1000), rng.randrange(1500000, 4000000, 5000),
                            rng.randrange(4000000, 30000000, 10000)])
        basic = int(gross * rng.choice([0.4, 0.5])) // 100 * 100
        has_hra = rng.random() < 0.6
        hra = int(basic * rng.choice([0.4, 0.5])) // 100 * 100 if has_hra else 0
        rent = rng.randrange(120000, 900000, 6000) if has_hra and rng.random() < 0.85 else 0
        inp = {"financial_year": fy, "age": rng.choice([24, 28, 33, 41, 52, 58, 61, 67, 81]),
               "gross_salary": gross, "basic_plus_da": basic if has_hra else 0, "hra_received": hra,
               "annual_rent_paid": rent, "metro_city": rng.choice(cities_metro) if has_hra else False,
               "section_80c": rng.choice([0, 50000, 100000, 150000, 200000]),
               "section_80ccd_1b": rng.choice([0, 0, 25000, 50000]),
               "health_insurance_self_family": rng.choice([0, 12000, 25000, 35000]),
               "preventive_checkup_self_family": rng.choice([0, 0, 2500, 5000]),
               "health_insurance_parents": rng.choice([0, 0, 20000, 45000]),
               "parents_are_senior": rng.random() < 0.5,
               "home_loan_interest_self_occupied": rng.choice([0, 0, 0, 150000, 240000])}
        out.append(_case(f"REAL-{i:03d}", "realistic", "Seeded realistic profile", inp))
    return out


def hand_worked_cases() -> list[dict]:
    out = []
    for h in HAND_WORKED:
        c = _case(h["case_id"], "hand_worked", h["derivation"][:80], h["input"],
                  hand_expected_tax_payable=h["hand"], derivation=h["derivation"])
        c["expected_source"] = "hand_worked+reference_oracle"
        out.append(c)
    return out


OFFICIAL_CHECK_IDS_PREFIX = ("HW-", "BND-202425-87A", "BND-HRA", "BND-CAP-80D", "BND-202425-SC-NEW-5000000", "REAL-0")


def official_worksheet(cases: list[dict], n: int = 30) -> list[dict]:
    """Pick 30 diverse FY 2024-25 cases for manual checking on the official calculator."""
    chosen: list[dict] = []
    for prefix in OFFICIAL_CHECK_IDS_PREFIX:
        for c in cases:
            if c["case_id"].startswith(prefix) and c["input"]["financial_year"] == "2024-25" and c not in chosen:
                chosen.append(c)
                if sum(1 for x in chosen if x["case_id"].startswith(prefix)) >= 6:
                    break
        if len(chosen) >= n:
            break
    rows = []
    for c in chosen[:n]:
        e = c["expected"]
        rows.append({"case_id": c["case_id"], "assessment_year": "2025-26",
                     "inputs_json": json.dumps(c["input"], sort_keys=True),
                     "our_old_tax_payable": e["old"]["tax_payable"], "our_new_tax_payable": e["new"]["tax_payable"],
                     "official_old_tax": "", "official_new_tax": "", "checked_by": "", "checked_on": "",
                     "status": "PENDING_MANUAL_CHECK"})
    return rows


def generate(seed: int = 20231, n_realistic: int = 60) -> dict[str, Any]:
    cases = hand_worked_cases() + boundary_cases() + realistic_cases(n_realistic, seed)
    ids = [c["case_id"] for c in cases]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    PA_DATA.mkdir(parents=True, exist_ok=True)
    (PA_DATA / "tax_test_cases.json").write_text(json.dumps({"generator": "project_a.test_case_generator",
                                                              "seed": seed, "n_cases": len(cases),
                                                              "cases": cases}, indent=1), encoding="utf-8")
    ws_path = PA_DATA / "official_calculator_worksheet.csv"
    existing = {}
    if ws_path.exists():  # never overwrite manual results already entered
        with open(ws_path, encoding="utf-8") as fh:
            existing = {r["case_id"]: r for r in csv.DictReader(fh)}
    rows = official_worksheet(cases)
    fields = list(rows[0].keys())
    for r in rows:
        prev = existing.get(r["case_id"])
        # keep official results (and any evidence/difference columns) only if the case inputs are unchanged
        if prev and prev.get("official_old_tax") and prev.get("inputs_json") == r["inputs_json"]:
            for k, v in prev.items():
                if k not in ("case_id", "assessment_year", "inputs_json", "our_old_tax_payable", "our_new_tax_payable"):
                    r[k] = v
                    if k not in fields:
                        fields.append(k)
    with open(ws_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, restval="")
        w.writeheader()
        w.writerows(rows)
    by_cat: dict[str, int] = {}
    for c in cases:
        by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
    return {"n_cases": len(cases), "by_category": by_cat, "worksheet_rows": len(rows)}


if __name__ == "__main__":
    print(json.dumps(generate(), indent=2))
