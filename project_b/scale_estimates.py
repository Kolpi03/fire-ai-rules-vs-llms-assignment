"""Task B6 projections: cost and latency of the hybrid pipeline at 50M rows/day across 120 customers.

All inputs are either MEASURED (read from benchmarks/project_b/*.json and the data sample) or an explicit
ASSUMPTION listed in ``ASSUMPTIONS``. The output (benchmarks/project_b/scale_estimates.json) is what
FINAL_DESIGN_B.md quotes.
"""
from __future__ import annotations

import json
from typing import Any

from common.config import settings
from common.paths import BENCH_B
from project_b.data_loader import PB_RAW, SAMPLE_PARQUET, load_sample

ASSUMPTIONS = {
    "rows_per_day": 50_000_000,
    "customers": 120,
    "peak_to_average": 4.0,                   # intraday peak rate vs daily average
    "production_overhead_factor": 3.0,        # parsing/serialisation/IO on top of measured detector CPU
    "cpu_usd_per_hour": None,                 # filled from settings.yaml (same price used in the benchmark)
    "llm_semantic_review_share_of_flagged": 0.10,  # share of ML-flagged semantic rows sent to the LLM (borderline)
    "llm_output_tokens_per_row": 6,           # row-review responses list only flagged ids + short reasons
}


def estimate() -> dict[str, Any]:
    det = json.loads((BENCH_B / "detector_results.json").read_text())
    hyb = json.loads((BENCH_B / "hybrid_results.json").read_text())
    a = dict(ASSUMPTIONS)
    a["cpu_usd_per_hour"] = settings()["llm"]["compute_usd_per_cpu_hour"]
    rows = a["rows_per_day"]
    thr = hyb["throughput_rows_per_s"]                        # measured, single thread, whole hybrid
    cpu_s_day = rows / thr * a["production_overhead_factor"]
    avg_rps = rows / 86400
    peak_rps = avg_rps * a["peak_to_average"]
    cores_peak = peak_rps / thr * a["production_overhead_factor"]
    # text uniqueness -> cache hit rate for any per-text LLM review
    df = load_sample()
    n = len(df)
    uniq_text = df["resolution_description"].nunique()
    uniq_pair = df[["status", "resolution_description"]].drop_duplicates().shape[0]
    uniq_type_desc = df[["complaint_type", "descriptor"]].drop_duplicates().shape[0]
    # measured flag rate of the deterministic pipeline on clean data + real-issue prevalence for semantic problems
    clean_fa = hyb["clean_row_false_alarm_rate"]
    raw_files = list(PB_RAW.glob("day_*.csv"))
    csv_bytes_per_row = (sum(f.stat().st_size for f in raw_files) / n) if raw_files else None
    parquet_bytes_per_row = SAMPLE_PARQUET.stat().st_size / n
    llm_rows = {r["problem"]: r for r in det["results"] if r["family"] == "llm"}
    per_row_cost = {p: r["projected_cost_per_million_rows_usd"] for p, r in llm_rows.items()
                    if r["algorithm"] == "LLM row review"}
    naive_llm_all_rows_day = sum(per_row_cost.values()) * rows / 1e6
    semantic = ("complaint_descriptor_mismatch", "resolution_status_contradiction", "pii_in_text")
    sem_cost_per_row = sum(per_row_cost[p] for p in semantic) / 1e6
    flagged_semantic_rows = rows * max(clean_fa, 0.001)   # at least 0.1% of rows reach stage 3 flags
    llm_rows_day = flagged_semantic_rows * a["llm_semantic_review_share_of_flagged"]
    hybrid_llm_day = llm_rows_day * sem_cost_per_row
    cached_unique_share = uniq_pair / n
    out = {
        "assumptions": a,
        "measured": {"hybrid_throughput_rows_per_s_single_thread": thr,
                     "hybrid_compute_usd_per_million_rows": hyb["cost_per_million_rows_usd_compute"],
                     "clean_row_false_alarm_rate": clean_fa, "stage_seconds_share": hyb["stage_seconds_share"],
                     "sample_rows": n, "unique_resolution_texts": uniq_text, "unique_status_text_pairs": uniq_pair,
                     "unique_type_descriptor_pairs": uniq_type_desc,
                     "raw_csv_bytes_per_row": csv_bytes_per_row, "parquet_bytes_per_row": parquet_bytes_per_row,
                     "projected_llm_row_review_usd_per_million_rows": per_row_cost},
        "projection": {
            "avg_rows_per_s": avg_rps, "peak_rows_per_s": peak_rps,
            "cpu_core_seconds_per_day": cpu_s_day, "cpu_cores_at_peak": cores_peak,
            "compute_usd_per_day": cpu_s_day / 3600 * a["cpu_usd_per_hour"],
            "llm_every_row_every_check_usd_per_day": naive_llm_all_rows_day,
            "llm_hybrid_stage4_rows_per_day": llm_rows_day, "llm_hybrid_stage4_usd_per_day": hybrid_llm_day,
            "llm_with_text_cache_unique_share": cached_unique_share,
            "llm_hybrid_stage4_with_cache_usd_per_day": hybrid_llm_day * cached_unique_share,
            "row_alerts_per_day_from_false_alarms": rows * clean_fa,
            "ingest_gb_per_day_csv": rows * csv_bytes_per_row / 1e9 if csv_bytes_per_row else None,
            "storage_gb_per_day_parquet": rows * parquet_bytes_per_row / 1e9,
        },
    }
    (BENCH_B / "scale_estimates.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    print(json.dumps(estimate(), indent=1, default=str))
