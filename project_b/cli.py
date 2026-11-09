"""Project B command line.

  python -m project_b.cli download            # NYC 311 2023 sample (cached) + full-year aggregates
  python -m project_b.cli profile             # Task B1 real-data profile -> data/reports/profile.{json,csv,md}
  python -m project_b.cli inject --problem pii_in_text --rate 0.05 --seed 1 --out /tmp/x.parquet
  python -m project_b.cli benchmark           # Task B4: 12 problems x 3 families
  python -m project_b.cli hybrid              # Task B5: end-to-end hybrid evaluation (+ variants)
  python -m project_b.cli check FILE.parquet  # run the hybrid pipeline on any batch file and print alerts
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="project_b", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("download")
    sub.add_parser("profile")
    s = sub.add_parser("inject")
    s.add_argument("--problem", required=True)
    s.add_argument("--rate", type=float)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out", required=True)
    sub.add_parser("benchmark")
    sub.add_parser("hybrid")
    s = sub.add_parser("check")
    s.add_argument("file")
    a = ap.parse_args(argv)

    if a.cmd == "download":
        from project_b import data_loader
        data_loader.download_sample()
        print(data_loader.build_processed(force=True))
        print(json.dumps(data_loader.fetch_full_year_aggregates(), indent=1))
    elif a.cmd == "profile":
        from project_b.profiler import run_profile
        for i in run_profile()["issues"]:
            print(f"{i['issue']:<42} {i['affected_rows']:>8,} {i['pct_of_sample'] * 100:6.2f}%")
    elif a.cmd == "inject":
        from project_b.benchmark import sample_batch
        from project_b.corruption_injector import inject
        from project_b.reference import prepare
        ref, _, pool = prepare()
        res = inject(sample_batch(pool, 5000, a.seed), a.problem, ref, a.rate, a.seed)
        res.batch.assign(_label=res.labels.to_numpy()).to_parquet(a.out, index=False)
        print(json.dumps(res.meta, default=str))
    elif a.cmd == "benchmark":
        from project_b.benchmark import run
        for r in run()["results"]:
            print(f"{r['problem']:<34} {r['family']:<12} F1={r.get('f1')} {r['status']}")
    elif a.cmd == "hybrid":
        print(json.dumps(run_hybrid(), indent=1, default=str)[:5000])
    elif a.cmd == "check":
        import pandas as pd
        from project_b.benchmark import build_families
        from project_b.hybrid_pipeline import HybridPipeline, select_winners
        from project_b.reference import prepare
        from common.paths import BENCH_B
        ref, ref_pool, _ = prepare()
        results = json.loads((BENCH_B / "detector_results.json").read_text())["results"]
        pipe = HybridPipeline(build_families(ref, ref_pool), select_winners(results))
        res = pipe.run(pd.read_parquet(a.file).drop(columns=["_label"], errors="ignore"))
        print(f"quarantined={res.quarantined}; flagged rows per problem:")
        print(res.flags.sum().to_string())
        for al in res.alerts[:20]:
            print(f"- [{al.problem}] {al.reason} (row={al.row}, column={al.column})")
    return 0


def run_hybrid() -> dict:
    from common.paths import BENCH_B
    from project_b.benchmark import build_families
    from project_b.hybrid_pipeline import compare_variants, evaluate_hybrid
    from project_b.reference import prepare
    ref, ref_pool, eval_pool = prepare()
    results = json.loads((BENCH_B / "detector_results.json").read_text())["results"]
    fams = build_families(ref, ref_pool)
    out = evaluate_hybrid(fams, ref, eval_pool, results)
    out["variants"] = compare_variants(fams, ref, eval_pool, results)
    (BENCH_B / "hybrid_results.json").write_text(json.dumps(out, indent=1, default=str))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
