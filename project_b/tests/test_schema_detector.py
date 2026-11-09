"""Family A rule detectors: each catches its injected problem and is quiet on clean data."""
import pytest

from project_b.corruption_injector import inject
from project_b.detection_types import BATCH_LEVEL_PROBLEMS
from project_b.evaluation import Confusion
from project_b.schema_detector import RULE_DETECTORS

DETS = {cls.problem: cls for cls in RULE_DETECTORS}
# PII regex and the status lexicon are measured weak baselines (see RESULTS_B), so only sanity-checked here.
STRONG = ["date_format_switch", "lat_lon_swap", "exact_duplicates", "closed_before_created", "zip_borough_mismatch",
          "complaint_descriptor_mismatch"]


def _det(problem, reference):
    d = DETS[problem]()
    return d.fit(reference) if hasattr(d, "fit") else d


@pytest.mark.parametrize("problem", sorted(BATCH_LEVEL_PROBLEMS))
def test_batch_level_rules(problem, batch, reference):
    d = _det(problem, reference)
    assert d.detect(inject(batch, problem, reference, seed=4).batch).batch_flag
    assert not d.detect(batch).batch_flag


@pytest.mark.parametrize("problem", STRONG)
def test_row_rules_perfect_on_synthetic(problem, batch, reference):
    d = _det(problem, reference)
    inj = inject(batch, problem, reference, seed=4)
    c = Confusion()
    c.add(d.detect(inj.batch).row_flags, inj.labels)
    assert c.recall == 1.0 and c.precision == 1.0
    assert not d.detect(batch).row_flags.any()


def test_null_contract_flags_spike_in_monitored_columns(batch, reference):
    d = _det("null_spike", reference)
    seen = set()
    for seed in range(40):
        inj = inject(batch, "null_spike", reference, rate=0.9, seed=seed)
        col = inj.meta["column"]
        flagged = d.detect(inj.batch).row_flags[inj.labels]
        if col == "location_type":
            assert not flagged.any()  # documented gap: naturally-null column has no fixed contract limit
        else:
            assert flagged.all()
        seen.add(col == "location_type")
    assert seen == {True, False}


def test_pii_regex_finds_phone_and_email_but_not_bare_names(batch, reference):
    d = _det("pii_in_text", reference)
    b = batch.head(3).copy()
    b.loc[0, "resolution_description"] = "Call back 212-555-0147."
    b.loc[1, "resolution_description"] = "Sent to jane.doe@gmail.com."
    b.loc[2, "resolution_description"] = "Maria Gonzalez at the location was informed."
    flags = d.detect(b).row_flags
    assert flags.tolist() == [True, True, False]  # documented weakness: bare names


def test_alerts_are_structured(batch, reference):
    d = _det("closed_before_created", reference)
    res = d.detect(inject(batch, "closed_before_created", reference, seed=4).batch)
    a = res.alerts[0]
    assert a.row is not None and a.observed and a.expected and a.explanation_quality() == 5
