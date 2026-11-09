"""Hybrid pipeline: winner selection, quarantine, repair, end-to-end flags."""
import pytest

from project_b.corruption_injector import inject, inject_many
from project_b.detection_types import BATCH_LEVEL_PROBLEMS, PROBLEMS
from project_b.evaluation import Confusion
from project_b.hybrid_pipeline import HybridPipeline, select_winners
from project_b.llm_detector import build_family_c
from project_b.ml_detector import build_family_b
from project_b.schema_detector import RULE_DETECTORS


@pytest.fixture(scope="module")
def families(reference, ref_frame):
    rule = {}
    for cls in RULE_DETECTORS:
        d = cls()
        rule[d.problem] = d.fit(reference) if hasattr(d, "fit") else d
    return {"rule": rule, "stat_ml": build_family_b(reference, ref_frame), "llm": build_family_c(reference.columns)}


def _fake_results(families):
    out = []
    for p in PROBLEMS:
        for fam, f1, fa, cost in (("rule", 0.9, 0.0, 0.001), ("stat_ml", 0.95, 0.0005, 0.002)):
            d = families[fam][p]
            out.append({"problem": p, "detector": d.name, "family": d.family, "status": "measured", "f1": f1,
                        "clean_row_false_alarm_rate": fa, "cost_per_million_rows_usd": cost})
    return out


def test_winner_selection_prefers_f1_then_cost(families):
    res = _fake_results(families)
    w = select_winners(res)
    assert all(v["family"] != "rule" for v in w.values())  # 0.95 > 0.9 + tie margin
    for r in res:
        if r["family"] != "rule":
            r["clean_row_false_alarm_rate"] = 0.01  # now violates the false-alarm constraint
    assert all(v["family"] == "rule" for v in select_winners(res).values())


def test_schema_drift_quarantines(families, batch, reference):
    pipe = HybridPipeline(families, select_winners(_fake_results(families)), use_llm=False)
    res = pipe.run(inject(batch, "schema_drift", reference, seed=1).batch)
    assert res.quarantined and not res.flags.any().any()


def test_type_change_is_flagged_then_repaired(families, batch, reference):
    pipe = HybridPipeline(families, select_winners(_fake_results(families)), use_llm=False)
    res = pipe.run(inject(batch, "type_change", reference, seed=1).batch)
    assert not res.quarantined and res.flags["type_change"].any()
    assert not res.flags["lat_lon_swap"].any()  # coerced numbers do not trigger coordinate checks


def test_end_to_end_mixed_batch(families, batch, reference):
    pipe = HybridPipeline(families, select_winners(_fake_results(families)), use_llm=True)
    probs = [p for p in PROBLEMS if p not in BATCH_LEVEL_PROBLEMS]
    mixed, labels, _ = inject_many(batch, probs, reference, seed=3, rates={p: 0.02 for p in probs})
    res = pipe.run(mixed)
    c = Confusion()
    c.add(res.flags[probs].any(axis=1), labels[probs].any(axis=1))
    assert c.recall > 0.9 and c.precision > 0.9, c.as_dict()
    assert res.llm["stage4"].startswith(("skipped", "ran"))


def test_clean_batch_is_quiet(families, batch):
    pipe = HybridPipeline(families, select_winners(_fake_results(families)), use_llm=False)
    res = pipe.run(batch)
    assert res.flags.any(axis=1).mean() < 0.01
