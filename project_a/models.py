"""Typed input/output models shared by all three tax engines.

Design notes
------------
* All money inputs are **annual, whole rupees** (``int`` >= 0). The natural-language parser is
  responsible for converting "25k per month" into 300000; engines never guess periods.
* Outputs use :class:`decimal.Decimal` so that 5% of an odd number of tens (e.g. Rs 0.50)
  is represented exactly. Only the final ``tax_payable`` is rounded (Section 288B).
* Every output line carries ``clause`` + ``rule_id`` + ``rule_version`` so each rupee can be traced
  to a legal provision (the traceability metric in Task A4 checks exactly these fields).
"""
from __future__ import annotations

from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

FinancialYear = Literal["2024-25", "2025-26"]
SUPPORTED_FYS: tuple[str, ...] = ("2024-25", "2025-26")


class AgeBand(str, Enum):
    BELOW_60 = "below_60"
    SENIOR_60_79 = "senior_60_79"
    SUPER_SENIOR_80_PLUS = "super_senior_80_plus"


def age_band(age: int) -> AgeBand:
    """Age is taken as the age reached at any time during the previous year (i.e. by 31 March)."""
    if age >= 80:
        return AgeBand.SUPER_SENIOR_80_PLUS
    if age >= 60:
        return AgeBand.SENIOR_60_79
    return AgeBand.BELOW_60


Money = int  # annual whole rupees


class TaxpayerProfile(BaseModel):
    """Structured, validated input for a salaried resident/non-resident individual."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    financial_year: FinancialYear = "2024-25"
    age: int = Field(ge=18, le=120, description="Age reached during the financial year")
    is_resident: bool = True

    gross_salary: Money = Field(ge=0, le=10_000_000_000, description="Annual gross salary incl. HRA and allowances")
    basic_plus_da: Money = Field(0, ge=0, description="Basic + DA forming part of retirement benefits (Rule 2A 'salary')")
    hra_received: Money = Field(0, ge=0)
    annual_rent_paid: Money = Field(0, ge=0)
    metro_city: bool = False  # Delhi, Mumbai, Kolkata, Chennai

    section_80c: Money = Field(0, ge=0)
    section_80ccd_1b: Money = Field(0, ge=0)

    health_insurance_self_family: Money = Field(0, ge=0)
    preventive_checkup_self_family: Money = Field(0, ge=0)
    medical_expenditure_self_family: Money = Field(0, ge=0, description="Only for an uninsured senior citizen")
    spouse_is_senior: bool = False

    health_insurance_parents: Money = Field(0, ge=0)
    preventive_checkup_parents: Money = Field(0, ge=0)
    medical_expenditure_parents: Money = Field(0, ge=0, description="Only for uninsured senior-citizen parents")
    parents_are_senior: bool = False

    # --- Section 80D per-person detail (optional) -------------------------------------------------------
    # s.80D(2)(c)/(d), first proviso: medical expenditure counts only if it is for a senior citizen AND no
    # insurance premium has been paid for the health of *such person*. The group fields above cannot say
    # which member is insured, so the legacy group amounts keep the conservative rule (they count only when
    # no premium is paid for anyone in the group). These per-person fields let an uninsured senior's
    # expenses count even when another member of the same group is insured.
    self_insured: bool = Field(False, description="The assessee is covered by a health-insurance premium paid")
    spouse_insured: bool = Field(False, description="The spouse is covered by a health-insurance premium paid")
    medical_expenditure_self: Money = Field(0, ge=0, description="Medical expenditure on the assessee")
    medical_expenditure_spouse: Money = Field(0, ge=0, description="Medical expenditure on the spouse")
    parent_a_is_senior: bool = False
    parent_a_insured: bool = False
    medical_expenditure_parent_a: Money = Field(0, ge=0)
    parent_b_is_senior: bool = False
    parent_b_insured: bool = False
    medical_expenditure_parent_b: Money = Field(0, ge=0)

    home_loan_interest_self_occupied: Money = Field(0, ge=0)

    @model_validator(mode="after")
    def _80d_per_person(self) -> "TaxpayerProfile":
        """Ask, don't guess: per-person 80D inputs must be unambiguous and must not double count."""
        given = self.model_fields_set
        groups = (
            ("self/family", self.health_insurance_self_family, self.medical_expenditure_self_family,
             (("self", self.medical_expenditure_self, self.self_insured, "self_insured"),
              ("spouse", self.medical_expenditure_spouse, self.spouse_insured, "spouse_insured"))),
            ("parents", self.health_insurance_parents, self.medical_expenditure_parents,
             (("parent_a", self.medical_expenditure_parent_a, self.parent_a_insured, "parent_a_insured"),
              ("parent_b", self.medical_expenditure_parent_b, self.parent_b_insured, "parent_b_insured"))),
        )
        for name, premium, legacy_medical, members in groups:
            if legacy_medical and any(m[1] for m in members):
                raise ValueError(f"80D {name}: give medical expenditure either as the group amount or per person, "
                                 "not both (the same expense must not be counted twice)")
            for who, medical, insured, flag in members:
                if insured and premium == 0:
                    raise ValueError(f"80D {name}: {flag}=True but no health-insurance premium is paid for the group")
                if medical and premium > 0 and flag not in given:
                    raise ValueError(f"80D {name}: medical expenditure for {who} is given while a premium is paid for "
                                     f"the group; state {flag} (is that person insured?)")
        for who in ("parent_a", "parent_b"):
            if getattr(self, f"medical_expenditure_{who}") and f"{who}_is_senior" not in given:
                raise ValueError(f"80D parents: state {who}_is_senior for {who}'s medical expenditure")
            if getattr(self, f"{who}_is_senior") and not self.parents_are_senior:
                raise ValueError(f"80D parents: {who}_is_senior=True requires parents_are_senior=True")
        per_parent = {"parent_a_insured", "parent_b_insured"} & given
        if per_parent and self.health_insurance_parents > 0 and not (self.parent_a_insured or self.parent_b_insured):
            raise ValueError("80D parents: a premium is paid for parents but neither parent is marked insured")
        # s.80D(4): the Rs 50,000 premium limit depends on whether an INSURED person is a senior citizen. Once
        # insurance is given per person for a group, every senior's insurance status in that group must be stated.
        if {"self_insured", "spouse_insured"} & given and self.health_insurance_self_family > 0:
            if self.age >= 60 and "self_insured" not in given:
                raise ValueError("80D self/family: state self_insured (the assessee is a senior citizen)")
            if self.spouse_is_senior and "spouse_insured" not in given:
                raise ValueError("80D self/family: state spouse_insured (the spouse is a senior citizen)")
        if per_parent:
            if self.parents_are_senior and not (self.parent_a_is_senior or self.parent_b_is_senior):
                raise ValueError("80D parents: per-parent insurance given; state which parent is the senior citizen")
            for who in ("parent_a", "parent_b"):
                if getattr(self, f"{who}_is_senior") and self.health_insurance_parents > 0 \
                        and f"{who}_insured" not in given:
                    raise ValueError(f"80D parents: state {who}_insured ({who} is a senior citizen)")
        return self

    @model_validator(mode="after")
    def _consistency(self) -> "TaxpayerProfile":
        if self.hra_received > self.gross_salary:
            raise ValueError("hra_received cannot exceed gross_salary")
        if self.basic_plus_da > self.gross_salary:
            raise ValueError("basic_plus_da cannot exceed gross_salary")
        if self.basic_plus_da + self.hra_received > self.gross_salary:
            raise ValueError("basic_plus_da + hra_received cannot exceed gross_salary")
        if self.hra_received > 0 and self.basic_plus_da == 0:
            raise ValueError("basic_plus_da is required to compute the HRA exemption")
        return self

    @property
    def age_band(self) -> AgeBand:
        return age_band(self.age)


DERIVED_80D_INPUTS = ("insured_senior_self_family", "insured_senior_parents")


def insured_senior_flags(p: "TaxpayerProfile") -> dict[str, bool]:
    """s.80D(4): is a health-insurance premium in the group paid for a SENIOR citizen?

    * Per-person mode (any ``*_insured`` flag of the group was given): exactly the stated people.
    * Group-only input (legacy): nobody's status is known, so the premium is taken to cover every member of
      the group — the same assumption the medical-expenditure rule makes for group amounts (a group premium
      means its seniors are insured). This keeps every group-level result unchanged.
    """
    given = p.model_fields_set
    if {"self_insured", "spouse_insured"} & given:
        a = (p.age >= 60 and p.self_insured) or (p.spouse_is_senior and p.spouse_insured)
    else:
        a = p.health_insurance_self_family > 0 and (p.age >= 60 or p.spouse_is_senior)
    if {"parent_a_insured", "parent_b_insured"} & given:
        b = (p.parent_a_is_senior and p.parent_a_insured) or (p.parent_b_is_senior and p.parent_b_insured)
    else:
        b = p.health_insurance_parents > 0 and p.parents_are_senior
    return {"insured_senior_self_family": bool(a), "insured_senior_parents": bool(b)}


class EngineStatus(str, Enum):
    OK = "ok"                      # result produced and (for LLM engines) validated
    UNVERIFIED = "unverified"      # LLM output parsed but failed arithmetic/clause validation
    REJECTED = "rejected"          # LLM output disagreed with the deterministic verifier
    UNAVAILABLE = "unavailable"    # no API key / offline: no result is fabricated
    ERROR = "error"


class ComponentLine(BaseModel):
    """One traceable line of the computation."""

    name: str
    amount: Decimal
    clause: str
    rule_id: str
    rule_version: str
    explanation: str = ""

    @field_serializer("amount")
    def _ser_amount(self, v: Decimal) -> str:
        return f"{v:.2f}"


class RegimeResult(BaseModel):
    regime: Literal["old", "new"]
    gross_salary: Decimal
    total_income: Decimal          # after 288A rounding
    tax_on_income: Decimal         # slab tax before rebate
    rebate_87a: Decimal
    surcharge: Decimal             # after marginal relief
    cess: Decimal
    total_tax: Decimal             # exact, before 288B rounding
    tax_payable: Decimal           # after 288B rounding
    lines: list[ComponentLine]

    @field_serializer("gross_salary", "total_income", "tax_on_income", "rebate_87a", "surcharge",
                      "cess", "total_tax", "tax_payable")
    def _ser(self, v: Decimal) -> str:
        return f"{v:.2f}"

    def key_figures(self) -> dict[str, str]:
        return {k: f"{getattr(self, k):.2f}" for k in
                ("total_income", "tax_on_income", "rebate_87a", "surcharge", "cess", "total_tax", "tax_payable")}


class TaxComputation(BaseModel):
    engine: str
    status: EngineStatus = EngineStatus.OK
    financial_year: str
    rule_set_id: str = ""
    rule_set_version: str = ""
    old: RegimeResult | None = None
    new: RegimeResult | None = None
    recommended_regime: Literal["old", "new"] | None = None
    savings: Decimal | None = None
    notes: list[str] = Field(default_factory=list)
    llm_meta: dict | None = None   # tokens, latency, model, mode (real/mock) for LLM engines

    @field_serializer("savings")
    def _ser_savings(self, v: Decimal | None) -> str | None:
        return None if v is None else f"{v:.2f}"
