"""Plain-language parser: the 50 dev cases, the held-out set (no unsafe outputs), and core behaviours."""
import json

import pytest

from common.paths import PA_DATA
from project_a.natural_language_parser import FieldStatus, parse
from project_a.nl_evaluation import judge, load_cases

CASES = load_cases()
HOLDOUT = json.loads((PA_DATA / "natural_language_holdout.json").read_text())["cases"]


def test_fifty_cases_present():
    assert len(CASES) == 50
    styles = {c["style"] for c in CASES}
    assert any("hinglish" in s for s in styles) and any("contradictory" in s for s in styles)
    assert any("incomplete" in s for s in styles) and any("ambiguous" in s for s in styles)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_dev_case(case):
    outcome, detail = judge(case, parse(case["text"]))
    assert outcome == "correct", detail


@pytest.mark.parametrize("case", HOLDOUT, ids=[c["id"] for c in HOLDOUT])
def test_holdout_never_unsafe(case):
    """On unseen phrasing the parser may over-ask, but must never return a wrong profile."""
    outcome, detail = judge(case, parse(case["text"]))
    assert outcome != "unsafe", detail


def test_zero_vs_unknown():
    stated = parse("Salary 12 lakh per year, age 30, no investments, no rent.")
    silent = parse("Salary 12 lakh per year, age 30.")
    assert stated.fields["section_80c"].status == FieldStatus.STATED_ZERO
    assert silent.fields["section_80c"].status == FieldStatus.NOT_MENTIONED
    assert stated.profile.section_80c == silent.profile.section_80c == 0


def test_assignment_example_asks_instead_of_guessing():
    r = parse("I earn 15 lakh annually, pay 25k rent in Bangalore, invest 1.5 lakh in 80C and have medical insurance.")
    assert r.profile is None
    asked = {c.field for c in r.clarifications}
    assert {"age", "annual_rent_paid", "hra_received", "health_insurance_self_family"} <= asked
    assert r.fields["gross_salary"].value == 1_500_000
    assert r.fields["section_80c"].value == 150_000
    assert r.fields["metro_city"].value is False  # Bangalore is not a Rule 2A metro


@pytest.mark.parametrize("text,value", [("salary 18 LPA, age 30", 1_800_000), ("salary 1.2 crore per year, age 30", 12_000_000),
                                        ("salary 95 hazaar per month, age 30", 1_140_000),
                                        ("salary Rs. 32,00,000 per year, age 30", 3_200_000)])
def test_amount_units(text, value):
    assert parse(text).profile.gross_salary == value


def test_audit_trail_records_conversions():
    r = parse("Salary 1,20,000 per month, age 45, no rent, no investments.")
    assert any("per" in a or "/month" in a for a in r.audit)
    assert r.profile.gross_salary == 1_440_000


def test_metro_definition_is_rule_2a():
    r = parse("Salary 20 lakh per year, age 30, rent 30k per month in Gurgaon, HRA 4 lakh per year, basic 8 lakh per year.")
    assert r.profile.metro_city is False
