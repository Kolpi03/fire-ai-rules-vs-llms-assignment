"""LLM detectors: offline mode never fabricates; parsing/accounting with a scripted MOCK client."""
import json

import pytest

from common.llm_client import MockLLMClient
from project_b.corruption_injector import inject
from project_b.detection_types import PROBLEMS
from project_b.llm_detector import LLMReviewDetector, build_family_c, row_prompt


def test_offline_returns_unavailable_and_no_flags(monkeypatch, batch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for det in build_family_c(list(batch.columns)).values():
        res = det.detect(batch.head(30), rows=batch.index[:5]) if det.problem not in (
            "schema_drift", "type_change", "null_spike", "near_duplicates") else det.detect(batch.head(30))
        assert res.available is False and res.mode == "llm-unavailable"
        assert res.row_flags is None and not res.batch_flag


def test_projection_available_offline(batch):
    for p in PROBLEMS:
        calls, tin, tout = LLMReviewDetector(p, list(batch.columns)).projected_tokens(batch.head(200), batch.index[:40])
        assert calls >= 1 and tin > 0 and tout > 0


def test_row_review_parses_mock_answer(batch, reference):
    inj = inject(batch, "pii_in_text", reference, seed=1)
    pos = inj.labels[inj.labels].index[:3]
    rows = pos.append(inj.labels[~inj.labels].index[:3])

    def answer(prompt):
        recs = json.loads(prompt.split("Rows:\n", 1)[1].split("\nReturn", 1)[0])
        return {"flagged": [{"row": r["row"], "reason": "name in text"} for r in recs if r["row"] in set(pos)]}
    det = LLMReviewDetector("pii_in_text", list(batch.columns), client=MockLLMClient(answer))
    res = det.detect(inj.batch, rows=rows)
    assert res.mode == "llm-mock"  # mock results are labelled as such, never as real
    assert res.row_flags.loc[pos].all() and not res.row_flags.loc[rows.difference(pos)].any()
    assert res.llm_calls == 1 and res.rows_reviewed == 6


def test_batch_review_mock(batch, reference):
    det = LLMReviewDetector("null_spike", list(batch.columns), client=MockLLMClient([{"problem_found": True,
                                                                                     "column": "NYPD/incident_zip",
                                                                                     "reason": "spike"}]))
    res = det.detect(inject(batch, "null_spike", reference, seed=2).batch)
    assert res.batch_flag and res.alerts[0].column == "NYPD/incident_zip"


@pytest.mark.parametrize("problem", ["pii_in_text", "zip_borough_mismatch"])
def test_prompt_contains_only_needed_columns(problem, batch):
    p = row_prompt(problem, batch.head(2))
    assert "x_coordinate_state_plane" not in p and "unique_key" not in p


# --- response-shape validation (added after the first real-model run returned a bare list) ---------------------
def test_shape_accepts_bare_list_and_object():
    from project_b.llm_detector import _shape
    assert _shape("flagged")([{"row": 1}]) == {"flagged": [{"row": 1}]}
    assert _shape("flagged")({"flagged": [], "x": 1})["flagged"] == []


def test_shape_rejects_unexpected_payload():
    import pytest
    from common.llm_client import LLMResponseError
    from project_b.llm_detector import _batch_shape, _shape
    with pytest.raises(LLMResponseError):
        _shape("flagged")("not json object")
    with pytest.raises(LLMResponseError):
        _shape("flagged")({"flagged": "row 3"})
    with pytest.raises(LLMResponseError):
        _batch_shape({"reason": "missing problem_found"})
    assert _batch_shape([{"problem_found": False}]) == {"problem_found": False}


def test_row_ids_as_strings_are_accepted():
    from project_b.llm_detector import _as_int
    assert _as_int("12") == 12 and _as_int(7) == 7 and _as_int("x") is None and _as_int(None) is None
