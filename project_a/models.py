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

    home_loan_interest_self_occupied: Money = Field(0, ge=0)

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
