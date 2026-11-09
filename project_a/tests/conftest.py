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
