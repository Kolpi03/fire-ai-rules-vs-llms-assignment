import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from common.paths import PA_DATA  # noqa: E402


@pytest.fixture(scope="session")
def tax_cases():
    return json.loads((PA_DATA / "tax_test_cases.json").read_text())["cases"]


def cases_by(prefix_or_category: str):
    cases = json.loads((PA_DATA / "tax_test_cases.json").read_text())["cases"]
    return [c for c in cases if c["category"].startswith(prefix_or_category) or c["case_id"].startswith(prefix_or_category)]


CANDIDATE_DIR = ROOT / "project_a/compiled_rules/candidates"


def candidate_artifact(fy: str):
    """The 80D per-person candidate for a year (None if absent): verified here, approved only by a human."""
    path = CANDIDATE_DIR / f"fy{fy.replace('-', '_')}_80d_per_person.candidate.json"
    return json.loads(path.read_text()) if path.exists() else None


def run_approved_or_refusal(profile):
    """Run the APPROVED compiled artifact. Returns its result, or the string 'refused' when it raises
    UnsupportedInput — allowed only if the profile really sets a field the artifact never reads."""
    from project_a import llm_compiled_engine as lce
    art = lce.load_artifact(profile.financial_year)
    try:
        return lce.run_artifact(art, profile)
    except lce.UnsupportedInput:
        used = lce.referenced_names(art)
        from project_a.models import TaxpayerProfile
        ignored = [f for f, i in TaxpayerProfile.model_fields.items()
                   if f != "financial_year" and f not in used
                   and (f in profile.model_fields_set or getattr(profile, f) != i.default)]
        assert ignored, "UnsupportedInput raised although every set field is read by the artifact"
        return "refused"
