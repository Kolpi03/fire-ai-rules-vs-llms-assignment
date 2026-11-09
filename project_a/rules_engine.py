"""Engine A — hand-coded deterministic rules (the ground truth).

Computation order (identical for both regimes; regime differences come only from the YAML rule set):

1.  Gross salary (input).
2.  HRA exemption u/s 10(13A) + Rule 2A = least of
    (a) HRA received, (b) rent − 10% of (basic+DA), (c) 50%/40% of (basic+DA) for metro/other.
    Zero if no rent is paid. Not allowed under the new regime.
3.  Standard deduction u/s 16(ia) = min(configured amount, salary after exemptions).
4.  Income from house property: self-occupied home-loan interest u/s 24(b), capped (Rs 2 lakh);
    the loss is set off against salary (s.71(3A) cap is also Rs 2 lakh). Not allowed under new regime.
    Gross total income (GTI) = max(0, salary income − house-property loss).
5.  Chapter VI-A (old regime only): 80C (cap 1.5 lakh), 80CCD(1B) (cap 50k), 80D (see ``_section_80d``).
    Total VI-A deductions cannot exceed GTI (s.80A(2)).
6.  Total income rounded to the nearest Rs 10 (s.288A).
7.  Slab tax (old regime: by age band, senior bands only for residents; new regime: single table).
8.  Rebate u/s 87A (residents only): full rebate up to the cap when total income <= limit; in the new
    regime, marginal relief above the limit: rebate = tax − (total income − limit) when positive.
9.  Surcharge by income band, with marginal relief: tax + surcharge may not exceed the tax+surcharge
    at the band threshold plus the income above the threshold.
10. Health & education cess 4% on (tax after rebate + surcharge).
11. Tax payable rounded to the nearest Rs 10 (s.288B).
12. Recommend the regime with the lower tax payable (tie -> new regime, the statutory default).

Every step appends a :class:`ComponentLine` citing clause + rule_id + rule-set version.
Arithmetic uses :class:`decimal.Decimal` — no binary floating point anywhere.
"""
from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

from project_a.models import (AgeBand, ComponentLine, EngineStatus, RegimeResult, TaxComputation,
                              TaxpayerProfile)
from project_a.rule_registry import RuleSet, get_rule_set

D = Decimal
ZERO = D(0)
ENGINE_NAME = "rules_engine"


def round_to_10(x: Decimal) -> Decimal:
    """Sections 288A/288B: ignore paise, then round to nearest 10 (last digit >= 5 rounds up)."""
    n = int(x)  # x >= 0 here; int() truncates paise
    r = n % 10
    return D(n - r + (10 if r >= 5 else 0))


def slab_tax(income: Decimal, slabs: list[list[Any]]) -> Decimal:
    """Progressive tax: sum over slabs of rate * (portion of income inside [lo, hi))."""
    tax = ZERO
    for lo, hi, rate in slabs:
        lo_d = D(lo)
        if income <= lo_d:
            break
        top = income if hi is None else min(income, D(hi))
        tax += (top - lo_d) * D(rate)
    return tax


class _Lines:
    """Small helper that builds traceable lines tagged with the rule-set version."""

    def __init__(self, version: str) -> None:
        self.version = version
        self.items: list[ComponentLine] = []

    def add(self, name: str, amount: Decimal, cfg_or_clause: dict | str, rule_id: str | None = None,
            explanation: str = "") -> None:
        if isinstance(cfg_or_clause, dict):
            clause, rid = cfg_or_clause["clause"], cfg_or_clause["rule_id"]
        else:
            clause, rid = cfg_or_clause, rule_id or "INPUT"
        self.items.append(ComponentLine(name=name, amount=amount, clause=clause, rule_id=rid,
                                        rule_version=self.version, explanation=explanation))


def _hra_exemption(p: TaxpayerProfile, cfg: dict, L: _Lines) -> Decimal:
    hra_cfg = cfg["hra_exemption"]
    if p.hra_received == 0:
        return ZERO
    if not hra_cfg["allowed"]:
        L.add("HRA exemption (not available)", ZERO, hra_cfg,
              explanation=f"HRA of Rs {p.hra_received:,} is fully taxable under this regime")
        return ZERO
    if p.annual_rent_paid == 0:
        L.add("HRA exemption", ZERO, hra_cfg, explanation="No rent paid, so no exemption")
        return ZERO
    salary = D(p.basic_plus_da)
    pct = D(hra_cfg["metro_pct"] if p.metro_city else hra_cfg["non_metro_pct"])
    a = D(p.hra_received)
    b = max(ZERO, D(p.annual_rent_paid) - D(hra_cfg["rent_excess_over_salary_pct"]) * salary)
    c = pct * salary
    ex = min(a, b, c)
    L.add("HRA exemption", -ex, hra_cfg,
          explanation=(f"least of (a) HRA received {a:,.0f}, (b) rent {p.annual_rent_paid:,} − 10% of salary "
                       f"{salary:,.0f} = {b:,.2f}, (c) {pct * 100:.0f}% of salary = {c:,.2f}"))
    return ex


def _section_80d(p: TaxpayerProfile, dcfg: dict) -> tuple[Decimal, Decimal, str]:
    """Return (self/family deduction, parents deduction, explanation).

    Interpretation (documented in RESULTS_A.md):
    * Self/family cap = 25k, or 50k if the assessee or spouse is a senior citizen (s.80D(4)).
    * Parents cap = 25k, or 50k if a parent is a senior citizen.
    * Medical expenditure (clauses c/d) counts only for a senior-citizen group with NO insurance premium
      paid for that group, inside the same 50k aggregate (second proviso).
    * Preventive check-ups count inside each group's cap and are limited to 5k in aggregate (s.80D(2A)),
      allocated to whichever group still has headroom (maximises the lawful deduction).
    """
    self_senior = p.age_band != AgeBand.BELOW_60 or p.spouse_is_senior
    cap_a = D(dcfg["self_family_cap_senior"] if self_senior else dcfg["self_family_cap"])
    cap_b = D(dcfg["parents_cap_senior"] if p.parents_are_senior else dcfg["parents_cap"])
    med_a = D(p.medical_expenditure_self_family) if (self_senior and p.health_insurance_self_family == 0) else ZERO
    med_b = D(p.medical_expenditure_parents) if (p.parents_are_senior and p.health_insurance_parents == 0) else ZERO
    base_a = min(D(p.health_insurance_self_family) + med_a, cap_a)
    base_b = min(D(p.health_insurance_parents) + med_b, cap_b)
    prev_cap = D(dcfg["preventive_checkup_aggregate_cap"])
    alloc_a = min(D(p.preventive_checkup_self_family), cap_a - base_a, prev_cap)
    alloc_b = min(D(p.preventive_checkup_parents), cap_b - base_b, prev_cap - alloc_a)
    expl = (f"self/family: premium {p.health_insurance_self_family:,} + eligible medical {med_a:,.0f} "
            f"+ preventive {alloc_a:,.0f} (cap {cap_a:,.0f}); parents: premium {p.health_insurance_parents:,} "
            f"+ eligible medical {med_b:,.0f} + preventive {alloc_b:,.0f} (cap {cap_b:,.0f})")
    return base_a + alloc_a, base_b + alloc_b, expl


def _surcharge(total_income: Decimal, tax: Decimal, scfg: dict, slabs: list, L: _Lines) -> Decimal:
    bands = [b for b in scfg["bands"] if total_income > D(b["above"])]
    if not bands:
        return ZERO
    band = bands[-1]
    rate = D(band["rate"])
    threshold = D(band["above"])
    prev_rate = D(bands[-2]["rate"]) if len(bands) > 1 else ZERO
    gross_sc = tax * rate
    # Marginal relief: (tax + surcharge) <= tax_at_threshold * (1 + prev_rate) + (income − threshold)
    ceiling = slab_tax(threshold, slabs) * (1 + prev_rate) + (total_income - threshold)
    relief = max(ZERO, tax + gross_sc - ceiling)
    sc = gross_sc - relief
    L.add(f"Surcharge @ {rate * 100:.0f}%", gross_sc, scfg,
          explanation=f"total income exceeds Rs {threshold:,.0f}")
    if relief > 0:
        L.add("Marginal relief on surcharge", -relief, scfg,
              explanation=(f"tax+surcharge capped at tax at Rs {threshold:,.0f} incl. surcharge "
                           f"({slab_tax(threshold, slabs) * (1 + prev_rate):,.2f}) + income above threshold "
                           f"({total_income - threshold:,.0f})"))
    return sc


def compute_regime(p: TaxpayerProfile, rs: RuleSet, regime: str) -> RegimeResult:
    cfg = rs.regime(regime)
    L = _Lines(rs.version)
    gross = D(p.gross_salary)
    L.add("Gross salary", gross, "Section 17(1) (input)", "INPUT.SALARY")

    hra_ex = _hra_exemption(p, cfg, L)
    after_exempt = gross - hra_ex
    std_cfg = cfg["standard_deduction"]
    std = min(D(std_cfg["amount"]), after_exempt)
    L.add("Standard deduction", -std, std_cfg,
          explanation=f"min(Rs {std_cfg['amount']:,}, salary {after_exempt:,.0f})")
    salary_income = after_exempt - std

    hl_cfg = cfg["home_loan_interest_self_occupied"]
    hp_loss = ZERO
    if p.home_loan_interest_self_occupied > 0:
        if hl_cfg["allowed"]:
            hp_loss = min(D(p.home_loan_interest_self_occupied), D(hl_cfg["cap"]))
            L.add("Home-loan interest (self-occupied)", -hp_loss, hl_cfg,
                  explanation=f"min(interest {p.home_loan_interest_self_occupied:,}, cap {hl_cfg['cap']:,})")
        else:
            L.add("Home-loan interest (not available)", ZERO, hl_cfg)
    gti = max(ZERO, salary_income - hp_loss)

    via = ZERO
    dcfg = cfg["deductions"]
    if dcfg.get("chapter_via_allowed", True):
        c80 = min(D(p.section_80c), D(dcfg["section_80c"]["cap"]))
        c1b = min(D(p.section_80ccd_1b), D(dcfg["section_80ccd_1b"]["cap"]))
        d_a, d_b, d_expl = _section_80d(p, dcfg["section_80d"])
        if p.section_80c:
            L.add("Section 80C", -c80, dcfg["section_80c"], explanation=f"min({p.section_80c:,}, cap)")
        if p.section_80ccd_1b:
            L.add("Section 80CCD(1B)", -c1b, dcfg["section_80ccd_1b"], explanation=f"min({p.section_80ccd_1b:,}, cap)")
        if d_a + d_b > 0:
            L.add("Section 80D", -(d_a + d_b), dcfg["section_80d"], explanation=d_expl)
        via = c80 + c1b + d_a + d_b
        if via > gti:
            L.add("Chapter VI-A limited to gross total income", via - gti, "Section 80A(2)", "OLD.80A2")
            via = gti
    elif any((p.section_80c, p.section_80ccd_1b, p.health_insurance_self_family, p.health_insurance_parents,
              p.preventive_checkup_self_family, p.preventive_checkup_parents)):
        L.add("Chapter VI-A deductions (not available)", ZERO, dcfg)

    rnd = rs.raw["rounding"]
    total_income = round_to_10(gti - via)
    L.add("Total income (rounded)", total_income, rnd["total_income"]["clause"], rnd["total_income"]["rule_id"],
          explanation=f"{gti - via:,.0f} rounded to nearest Rs 10")

    slab_cfg = cfg["slabs"]
    if regime == "old":
        band = p.age_band.value if p.is_resident else AgeBand.BELOW_60.value
    else:
        band = "all"
    slabs = slab_cfg["by_age_band"][band]
    tax = slab_tax(total_income, slabs)
    L.add("Tax on total income (slab rates)", tax, slab_cfg, explanation=f"slab table '{band}'")

    rcfg = cfg["rebate_87a"]
    limit, cap = D(rcfg["income_limit"]), D(rcfg["max_rebate"])
    rebate = ZERO
    if p.is_resident or not rcfg["resident_only"]:
        if total_income <= limit:
            rebate = min(tax, cap)
            expl = f"total income <= Rs {limit:,.0f}: min(tax, Rs {cap:,.0f})"
        elif rcfg["marginal_relief"] and tax > total_income - limit:
            rebate = tax - (total_income - limit)
            expl = f"marginal relief: tax {tax:,.2f} − income above Rs {limit:,.0f} ({total_income - limit:,.0f})"
        else:
            expl = "total income above the rebate limit"
        if rebate > 0:
            L.add("Rebate u/s 87A", -rebate, rcfg, explanation=expl)
    tax_after_rebate = tax - rebate

    surcharge = _surcharge(total_income, tax_after_rebate, cfg["surcharge"], slabs, L)
    cess = (tax_after_rebate + surcharge) * rs.cess_rate
    if cess:
        L.add("Health & education cess @ 4%", cess, rs.raw["cess"])
    total = tax_after_rebate + surcharge + cess
    payable = round_to_10(total)
    L.add("Tax payable (rounded)", payable, rnd["tax_payable"]["clause"], rnd["tax_payable"]["rule_id"],
          explanation=f"{total:,.2f} rounded to nearest Rs 10")
    return RegimeResult(regime=regime, gross_salary=gross, total_income=total_income, tax_on_income=tax,
                        rebate_87a=rebate, surcharge=surcharge, cess=cess, total_tax=total,
                        tax_payable=payable, lines=L.items)


def recommend(old: RegimeResult, new: RegimeResult, tie_breaker: str = "new") -> tuple[str, Decimal]:
    if old.tax_payable < new.tax_payable:
        return "old", new.tax_payable - old.tax_payable
    if new.tax_payable < old.tax_payable:
        return "new", old.tax_payable - new.tax_payable
    return tie_breaker, ZERO


def compute(p: TaxpayerProfile) -> TaxComputation:
    """Compute both regimes and the recommendation for one taxpayer."""
    t0 = time.perf_counter()
    rs = get_rule_set(p.financial_year)
    old = compute_regime(p, rs, "old")
    new = compute_regime(p, rs, "new")
    choice, savings = recommend(old, new, rs.raw["regime_choice"]["tie_breaker"])
    notes = []
    if not p.is_resident:
        notes.append("Non-resident: Section 87A rebate and senior-citizen exemption limits do not apply.")
    return TaxComputation(engine=ENGINE_NAME, status=EngineStatus.OK, financial_year=p.financial_year,
                          rule_set_id=rs.id, rule_set_version=rs.version, old=old, new=new,
                          recommended_regime=choice, savings=savings, notes=notes,
                          llm_meta={"mode": "deterministic", "latency_ms": (time.perf_counter() - t0) * 1000})
