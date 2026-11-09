"""Family B statistical / ML detectors."""
import pandas as pd
import pytest

from project_b.corruption_injector import inject
from project_b.evaluation import Confusion
from project_b.ml_detector import build_family_b


@pytest.fixture(scope="module")
def fam_b(reference, ref_frame):
    return build_family_b(reference, ref_frame)


@pytest.mark.parametrize("problem,min_recall,min_precision", [
    ("date_format_switch", 1.0, 1.0), ("lat_lon_swap", 1.0, 1.0), ("exact_duplicates", 1.0, 1.0),
    ("closed_before_created", 1.0, 1.0), ("zip_borough_mismatch", 0.95, 0.9),
    ("complaint_descriptor_mismatch", 0.9, 0.9),
    ("resolution_status_contradiction", 0.7, 0.9),  # sanity floor; measured real-data recall is in RESULTS_B
    ("pii_in_text", 0.95, 0.9), ("near_duplicates", 0.6, 0.9)])
def test_row_detectors(problem, min_recall, min_precision, fam_b, batch, reference):
    inj = inject(batch, problem, reference, seed=6)
    c = Confusion()
    c.add(fam_b[problem].detect(inj.batch).row_flags, inj.labels)
    assert c.recall >= min_recall and c.precision >= min_precision, c.as_dict()


@pytest.mark.parametrize("problem", ["schema_drift", "type_change", "null_spike"])
def test_batch_detectors(problem, fam_b, batch, reference):
    inj = inject(batch, problem, reference, seed=6)
    assert fam_b[problem].detect(inj.batch).batch_flag
    assert not fam_b[problem].detect(batch).batch_flag


def test_profile_matcher_diagnoses_rename(fam_b, batch):
    renamed = batch.rename(columns={"incident_zip": "zip_code"})
    res = fam_b["schema_drift"].detect(renamed)
    assert any("renamed to 'zip_code'" in a.reason for a in res.alerts)


def test_null_binomial_ignores_normal_null_rates(fam_b, batch):
    b = batch.copy()
    b.loc[b.index[:20], "location_type"] = pd.NA  # 1% nulls, well within noise for a 2,000-row batch
    assert not fam_b["null_spike"].detect(b).batch_flag


def test_pii_allowlist_ignores_agency_numbers(fam_b, batch, reference):
    det = fam_b["pii_in_text"]
    det.allow = det.allow | {"2126399675"}
    b = batch.head(2).copy()
    b.loc[b.index[0], "resolution_description"] = "Call the agency at (212) 639-9675 for status."
    b.loc[b.index[1], "resolution_description"] = "Caller reachable at 917-555-0199."
    assert det.detect(b).row_flags.tolist() == [False, True]
