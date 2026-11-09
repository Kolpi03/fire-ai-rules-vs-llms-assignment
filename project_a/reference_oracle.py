"""Independent reference oracle used ONLY to produce expected values for the test suite.

Why a second implementation?  If expected values came from ``rules_engine`` the tests would be
circular. This oracle is written independently (N-version style):

* no YAML and no shared helpers — every constant is written out per financial year below;
* exact rational arithmetic with :class:`fractions.Fraction` (Engine A uses ``Decimal``);
* slab tables expressed as "rate applies above threshold" pairs (Engine A uses [lo, hi, rate] rows);
* the 80D preventive-check-up cap is applied to the *sum* of usable amounts (Engine A allocates
  group by group).

Agreement between the oracle and Engine A is evidence of correctness, not proof (same author,
possible common-mode misreading of the law). The residual risk is covered by (1) hand-worked cases
in ``test_case_generator.HAND_WORKED`` with the arithmetic written out in the JSON, and (2) the
30-case official-calculator verification worksheet.

FY 2025-26 constants are present because the test suite is written *before* the engines are
migrated (Task A5 is test-first).
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction as F


@dataclass(frozen=True)
class _FY:
    old_slabs: dict[str, tuple[tuple[int, F], ...]]   # (threshold, rate) — rate applies ABOVE threshold
    new_slabs: tuple[tuple[int, F], ...]
    old_std: int
    new_std: int
    old_rebate: tuple[int, int]                       # (income limit, max rebate)
    new_rebate: tuple[int, int]
    old_surcharge: tuple[tuple[int, F], ...]          # (income above, rate)
    new_surcharge: tuple[tuple[int, F], ...]


def _pct(n: int) -> F:
    return F(n, 100)


_OLD_SLABS = {
    "below_60": ((250000, _pct(5)), (500000, _pct(20)), (1000000, _pct(30))),
    "senior_60_79": ((300000, _pct(5)), (500000, _pct(20)), (1000000, _pct(30))),
    "super_senior_80_plus": ((500000, _pct(20)), (1000000, _pct(30))),
}
_OLD_SC = ((5000000, _pct(10)), (10000000, _pct(15)), (20000000, _pct(25)), (50000000, _pct(37)))
_NEW_SC = ((5000000, _pct(10)), (10000000, _pct(15)), (20000000, _pct(25)))

FY_CONSTANTS: dict[str, _FY] = {
    "2024-25": _FY(
        old_slabs=_OLD_SLABS,
        new_slabs=((300000, _pct(5)), (700000, _pct(10)), (1000000, _pct(15)), (1200000, _pct(20)),
                   (1500000, _pct(30))),
        old_std=50000, new_std=75000,
        old_rebate=(500000, 12500), new_rebate=(700000, 25000),
        old_surcharge=_OLD_SC, new_surcharge=_NEW_SC),
    "2025-26": _FY(
        old_slabs=_OLD_SLABS,
        new_slabs=((400000, _pct(5)), (800000, _pct(10)), (1200000, _pct(15)), (1600000, _pct(20)),
                   (2000000, _pct(25)), (2400000, _pct(30))),
        old_std=50000, new_std=75000,
        old_rebate=(500000, 12500), new_rebate=(1200000, 60000),
        old_surcharge=_OLD_SC, new_surcharge=_NEW_SC),
}


def slab_tax(income: int, slabs: tuple[tuple[int, F], ...]) -> F:
    tax = F(0)
    for i, (thr, rate) in enumerate(slabs):
        nxt = slabs[i + 1][0] if i + 1 < len(slabs) else None
        if income > thr:
            tax += (min(income, nxt) - thr if nxt is not None else income - thr) * rate
    return tax


def round10(x: F) -> int:
    """288A/288B: drop paise, then nearest multiple of 10 with 5 rounding up."""
    rupees = int(x)  # floor for x >= 0
    return rupees - rupees % 10 + (10 if rupees % 10 >= 5 else 0)


def age_band(age: int) -> str:
    return "super_senior_80_plus" if age >= 80 else "senior_60_79" if age >= 60 else "below_60"


def deduction_80d(p: dict) -> int:
    senior_self = p.get("age", 30) >= 60 or p.get("spouse_is_senior", False)
    senior_par = p.get("parents_are_senior", False)
    cap_a, cap_b = (50000 if senior_self else 25000), (50000 if senior_par else 25000)
    ins_a, ins_b = p.get("health_insurance_self_family", 0), p.get("health_insurance_parents", 0)
    med_a = p.get("medical_expenditure_self_family", 0) if (senior_self and ins_a == 0) else 0
    med_b = p.get("medical_expenditure_parents", 0) if (senior_par and ins_b == 0) else 0
    base_a, base_b = min(ins_a + med_a, cap_a), min(ins_b + med_b, cap_b)
    usable_prev = (min(p.get("preventive_checkup_self_family", 0), cap_a - base_a)
                   + min(p.get("preventive_checkup_parents", 0), cap_b - base_b))
    return base_a + base_b + min(5000, usable_prev)


def regime_figures(p: dict, regime: str) -> dict[str, int | str]:
    """Expected figures for one regime (rupees; fractional amounts as 2-dp strings)."""
    fy = FY_CONSTANTS[p.get("financial_year", "2024-25")]
    resident = p.get("is_resident", True)
    gross = F(p["gross_salary"])

    hra = F(0)
    if regime == "old" and p.get("hra_received", 0) > 0 and p.get("annual_rent_paid", 0) > 0:
        sal = F(p["basic_plus_da"])
        hra = min(F(p["hra_received"]), max(F(0), p["annual_rent_paid"] - sal / 10),
                  sal * (_pct(50) if p.get("metro_city") else _pct(40)))
    after = gross - hra
    std = min(F(fy.old_std if regime == "old" else fy.new_std), after)
    hp_loss = F(min(p.get("home_loan_interest_self_occupied", 0), 200000)) if regime == "old" else F(0)
    gti = max(F(0), after - std - hp_loss)
    via = F(0)
    if regime == "old":
        via = min(gti, F(min(p.get("section_80c", 0), 150000) + min(p.get("section_80ccd_1b", 0), 50000)
                         + deduction_80d(p)))
    total_income = round10(gti - via)

    if regime == "old":
        slabs = fy.old_slabs[age_band(p["age"]) if resident else "below_60"]
        (r_limit, r_max), marginal, sc_bands = fy.old_rebate, False, fy.old_surcharge
    else:
        slabs = fy.new_slabs
        (r_limit, r_max), marginal, sc_bands = fy.new_rebate, True, fy.new_surcharge

    tax = slab_tax(total_income, slabs)
    rebate = F(0)
    if resident:
        if total_income <= r_limit:
            rebate = min(tax, F(r_max))
        elif marginal and tax > total_income - r_limit:
            rebate = tax - (total_income - r_limit)
    net = tax - rebate

    surcharge = F(0)
    crossed = [(thr, rate) for thr, rate in sc_bands if total_income > thr]
    if crossed:
        thr, rate = crossed[-1]
        prev_rate = crossed[-2][1] if len(crossed) > 1 else F(0)
        liability_at_threshold = slab_tax(thr, slabs) * (1 + prev_rate)
        surcharge = min(net * rate, max(F(0), liability_at_threshold + (total_income - thr) - net))
    cess = (net + surcharge) * _pct(4)
    total = net + surcharge + cess

    def s(x: F) -> str:  # 4 dp: components can carry sub-paisa fractions (e.g. 37% surcharge on x.50)
        return f"{float(x):.4f}"

    return {"total_income": total_income, "tax_on_income": s(tax), "rebate_87a": s(rebate),
            "surcharge": s(surcharge), "cess": s(cess), "total_tax": s(total), "tax_payable": round10(total)}


def expected(p: dict) -> dict:
    old, new = regime_figures(p, "old"), regime_figures(p, "new")
    rec = "old" if old["tax_payable"] < new["tax_payable"] else "new"  # tie -> new (default regime)
    return {"old": old, "new": new, "recommended_regime": rec,
            "savings": abs(int(old["tax_payable"]) - int(new["tax_payable"]))}
