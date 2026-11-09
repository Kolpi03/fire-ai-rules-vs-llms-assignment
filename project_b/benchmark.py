"""Task B4 benchmark: 12 problems x 3 detector families (+ alternatives), on seeded batches.

Steps
1. ``reference.prepare`` -> frozen reference + clean pools (reference months / evaluation months).
2. Batches: ``n_clean_batches`` clean and ``n_eval_batches`` corrupted batches per problem, each a fresh
   seeded sample of ``batch_size`` rows from the evaluation pool; corrupted batches, labels and metadata are
   written to ``data/corrupted/<problem>/`` so every number can be traced to a file.
3. Fit families A (rules), B (statistical/ML), C (LLM) and alternatives; evaluate.
4. Write ``benchmarks/project_b/detector_results.{json,csv}``.
"""
from __future__ import annotations

import json
import time
from typing import Any

import numpy as np
import pandas as pd

from common.config import load_yaml, settings
from common.llm_client import llm_status
from common.paths import BENCH_B, PB_CORRUPTED
from project_b.corruption_injector import InjectionResult, inject
from project_b.detection_types import PROBLEMS
from project_b.evaluation import evaluate_deterministic, evaluate_llm
from project_b.llm_detector import build_family_c
from project_b.ml_detector import IsolationForestCoords, build_family_b
from project_b.reference import Reference, prepare
from project_b.schema_detector import RULE_DETECTORS


def sample_batch(pool: pd.DataFrame, size: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(pool), size=min(size, len(pool)), replace=False)
    b = pool.iloc[np.sort(idx)].reset_index(drop=True)  # keep chronological order like a real feed
    return b


def make_batches(ref: Reference, eval_pool: pd.DataFrame, save: bool = True) -> tuple[dict[str, list[InjectionResult]], list[pd.DataFrame]]:
    cfg = settings()["project_b"]
    seed = load_yaml("benchmark_config.yaml")["project_b"]["seed"]
    clean = [sample_batch(eval_pool, cfg["batch_size"], seed * 1000 + i) for i in range(cfg["n_clean_batches"])]
    corrupted: dict[str, list[InjectionResult]] = {}
    for pi, p in enumerate(PROBLEMS):
        corrupted[p] = []
        for i in range(cfg["n_eval_batches"]):
            s = seed * 100000 + pi * 100 + i
            corrupted[p].append(inject(sample_batch(eval_pool, cfg["batch_size"], s), p, ref, seed=s))
    if save:
        for p, lst in corrupted.items():
            d = PB_CORRUPTED / p
            d.mkdir(parents=True, exist_ok=True)
            for i, inj in enumerate(lst):
                inj.batch.astype({c: "string" for c in inj.batch.columns if inj.batch[c].dtype == object}).to_parquet(
                    d / f"batch_{i:02d}.parquet", index=False)
                inj.labels.to_frame("label").to_parquet(d / f"labels_{i:02d}.parquet", index=False)
                (d / f"meta_{i:02d}.json").write_text(json.dumps(inj.meta, default=str))
        cd = PB_CORRUPTED / "_clean"
        cd.mkdir(parents=True, exist_ok=True)
        for i, b in enumerate(clean):
            b.to_parquet(cd / f"batch_{i:02d}.parquet", index=False)
    return corrupted, clean


def build_families(ref: Reference, ref_pool: pd.DataFrame) -> dict[str, dict[str, Any]]:
    fam_a = {}
    for cls in RULE_DETECTORS:
        d = cls()
        d = d.fit(ref) if hasattr(d, "fit") else d
        fam_a[d.problem] = d
    fam_b = build_family_b(ref, ref_pool)
    fam_c = build_family_c(ref.columns)
    alts = {"lat_lon_swap": [IsolationForestCoords().fit(ref_pool)]}
    return {"rule": fam_a, "stat_ml": fam_b, "llm": fam_c, "alternatives": alts}


def _previous_llm_rows() -> dict[str, dict[str, Any]]:
    """Stored *real* LLM-detector measurements, reused so a reproducibility run never replaces a real
    measurement with NOT MEASURED (offline) or with a fresh non-deterministic sample. LLM_REMEASURE=1 forces new calls."""
    import os
    path = BENCH_B / "detector_results.json"
    if os.environ.get("LLM_REMEASURE") == "1" or not path.exists():
        return {}
    prev = json.loads(path.read_text())
    out = {}
    for r in prev.get("results", []):
        if r.get("family") == "llm" and str(r.get("status", "")).startswith("measured"):
            out[r["problem"]] = {**r, "reused_from_run": r.get("reused_from_run") or prev.get("generated_at")}
    return out


def run(save_batches: bool = True) -> dict[str, Any]:
    t0 = time.time()
    ref, ref_pool, eval_pool = prepare()
    corrupted, clean = make_batches(ref, eval_pool, save=save_batches)
    fams = build_families(ref, ref_pool)
    llm_rows = settings()["project_b"]["llm_rows_per_problem"] // 2
    rows: list[dict[str, Any]] = []
    prev_llm = _previous_llm_rows()
    for p in PROBLEMS:
        rows.append(evaluate_deterministic(p, fams["rule"][p], corrupted[p], clean))
        rows.append(evaluate_deterministic(p, fams["stat_ml"][p], corrupted[p], clean))
        rows.append(prev_llm.get(p) or evaluate_llm(p, fams["llm"][p], corrupted[p], clean, llm_rows, seed=17))
    alt_rows = [dict(evaluate_deterministic(p, d, corrupted[p], clean), alternative=True)
                for p, ds in fams["alternatives"].items() for d in ds]
    llm_info = llm_status()
    if prev_llm and llm_info["mode"] != "real":
        llm_info = {**llm_info, "mode": "real (reused stored measurement)"}
    out = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "llm": llm_info,
           "config": settings()["project_b"], "reference_rows": len(ref_pool), "eval_pool_rows": len(eval_pool),
           "results": rows, "alternatives": alt_rows, "runtime_s": time.time() - t0}
    BENCH_B.mkdir(parents=True, exist_ok=True)
    (BENCH_B / "detector_results.json").write_text(json.dumps(out, indent=1, default=str))
    pd.DataFrame(rows + alt_rows).to_csv(BENCH_B / "detector_results.csv", index=False)
    return out


if __name__ == "__main__":
    res = run()
    for r in res["results"]:
        print(f"{r['problem']:<34} {r['family']:<12} P={r.get('precision')} R={r.get('recall')} "
              f"cleanFA={r.get('clean_row_false_alarm_rate')} {r['status']}")
