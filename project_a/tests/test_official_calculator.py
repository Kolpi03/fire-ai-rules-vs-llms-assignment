"""The official-calculator comparison (Task A1): structure and comparison rule, recomputed offline."""
import csv
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "evidence/official_calculator/portal_capture_raw.json"
pytestmark = pytest.mark.skipif(not RAW.exists(), reason="no official-calculator capture")


def test_every_worksheet_case_has_both_regimes_captured():
    rows = json.loads(RAW.read_text())["rows"]
    with open(ROOT / "project_a/data/official_calculator_worksheet.csv", encoding="utf-8") as fh:
        ws = list(csv.DictReader(fh))
    assert len(ws) == 30
    assert {(r[0], r[1]) for r in rows} == {(i, g) for i in range(1, 31) for g in ("old", "new")}
    for i in range(1, 31):
        for g in ("old", "new"):
            assert (ROOT / f"evidence/official_calculator/case_{i:02d}_{g}.png").exists()


def test_comparison_uses_288b_rounded_line_and_matches_engine():
    import sys
    sys.path.insert(0, str(ROOT))
    from scripts.official_calculator_compare import run
    s = run()
    assert s["cases_compared"] == 30
    for r in s["results"]:
        for g in ("old", "new"):
            reg = r["regimes"][g]
            assert reg["toggles_match_case"], (r["case_id"], g)
            # the unrounded portal total is within Rs 1 of ours; the s.288B line is what is compared
            assert abs(reg["difference_official_minus_ours"]["total_tax_before_288B"]) < 1.0 + 1e-9
            assert reg["status"] == ("MATCH" if reg["official"]["tax_payable_288B"] == reg["ours"]["tax_payable_288B"]
                                     else "MISMATCH")
