"""Corruption injector: every problem type, reproducibility, label correctness, original untouched."""
import pandas as pd
import pytest

from project_b.corruption_injector import DEFAULT_RATES, inject, inject_many
from project_b.detection_types import BATCH_LEVEL_PROBLEMS, PROBLEMS
from project_b.reference import parse_dates


@pytest.mark.parametrize("problem", PROBLEMS)
def test_each_problem_injects_and_labels(problem, batch, reference):
    original = batch.copy(deep=True)
    res = inject(batch, problem, reference, seed=3)
    pd.testing.assert_frame_equal(batch, original)  # original never mutated
    assert res.batch_label
    if problem not in BATCH_LEVEL_PROBLEMS:
        assert res.labels.sum() > 0
        assert len(res.labels) == len(res.batch)
    assert res.meta["problem"] == problem and res.meta["seed"] == 3


@pytest.mark.parametrize("problem", PROBLEMS)
def test_reproducible(problem, batch, reference):
    a, b = inject(batch, problem, reference, seed=11), inject(batch, problem, reference, seed=11)
    pd.testing.assert_frame_equal(a.batch, b.batch)
    assert a.labels.equals(b.labels)


def test_rate_is_respected(batch, reference):
    for rate in (0.01, 0.05):
        res = inject(batch, "lat_lon_swap", reference, rate=rate, seed=1)
        assert res.labels.sum() == round(rate * len(batch))


def test_labelled_rows_really_have_the_problem(batch, reference):
    r = inject(batch, "closed_before_created", reference, seed=2)
    lab = r.labels
    assert (parse_dates(r.batch.loc[lab, "closed_date"]) < parse_dates(r.batch.loc[lab, "created_date"])).all()
    r = inject(batch, "lat_lon_swap", reference, seed=2)
    assert (r.batch.loc[r.labels, "latitude"] < -70).all()
    r = inject(batch, "exact_duplicates", reference, seed=2)
    assert r.batch.astype(str).duplicated(keep="first")[r.labels].all()
    r = inject(batch, "zip_borough_mismatch", reference, seed=2)
    mapped = r.batch.loc[r.labels, "incident_zip"].map(reference.zip_borough)
    assert (mapped != r.batch.loc[r.labels, "borough"]).all()
    r = inject(batch, "date_format_switch", reference, seed=2)
    assert r.batch.loc[r.labels, "created_date"].str.match(r"\d{2}/\d{2}/\d{4}").all()
    assert r.labels.iloc[-1] and not r.labels.iloc[0]  # switch happens partway through


def test_null_spike_targets_one_agency(batch, reference):
    r = inject(batch, "null_spike", reference, seed=5)
    assert r.batch.loc[r.labels, "agency"].nunique() == 1


def test_defaults_cover_all_problems():
    assert set(DEFAULT_RATES) == set(PROBLEMS)


def test_inject_many_keeps_labels_aligned(batch, reference):
    probs = [p for p in PROBLEMS if p not in BATCH_LEVEL_PROBLEMS]
    out, labels, metas = inject_many(batch, probs, reference, seed=9, rates={p: 0.02 for p in probs})
    assert len(out) == len(labels) and len(metas) == len(probs)
    assert not any(c.startswith("_lbl_") for c in out.columns)
    for p in ("lat_lon_swap", "closed_before_created", "exact_duplicates"):
        assert labels[p].sum() > 0
    assert out.astype(str).duplicated(keep="first")[labels["exact_duplicates"]].all()
