"""Reproduce every reported number with one command:  python run_all.py

Steps (each is logged; a failed external step is reported and offline steps continue):
 1. environment check (Python, packages, disk, network, Gemini key presence — never the value)
 2. official tax-source snapshots (best effort; cached snapshots are used offline)
 3. generate the Project A test suite
 4. Project A tests (pytest)
 5. Project A benchmark (both financial years)
 6. Task A5 migration report (probes, silent-failure probe, mutation tests, differential fuzz)
 7. NYC 311 data: download if not cached (cached raw files are reused)
 8. Project B profile (Task B1)
 9. Project B tests (pytest)
10. Project B detector benchmark (12 problems x 3 families)
11. Project B hybrid pipeline evaluation (+ variants)
12. regenerate Markdown reports from the JSON results
13. validate the submission and write benchmarks/submission_summary.json

Options: --skip-download (offline: no NYC API calls; cached raw chunks and cached full-year counts are used),
         --quick (fewer Project B batches; NOT used for reported numbers),
         --resume (continue after the last step recorded "ok" in logs/run_all.checkpoint.json),
         --from-step N (run steps N..13 only; earlier steps are recorded as "skipped").
Every run writes logs/run_all.checkpoint.json after each step and logs/run_all.exitcode at the end.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from common.paths import ensure_dirs  # noqa: E402

STEPS: list[dict] = []
CHECKPOINT = ROOT / "logs" / "run_all.checkpoint.json"
EXITCODE = ROOT / "logs" / "run_all.exitcode"


def _save_checkpoint(meta: dict) -> None:
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT.write_text(json.dumps({**meta, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": STEPS},
                                     indent=1, default=str))


def step(name: str):
    def deco(fn):
        def run(*a, **k):
            t0 = time.time()
            print(f"\n=== {name} ===", flush=True)
            try:
                detail = fn(*a, **k)
                STEPS.append({"step": name, "status": "ok", "seconds": round(time.time() - t0, 1), "detail": detail})
            except Exception as exc:  # noqa: BLE001 - report and continue
                traceback.print_exc()
                STEPS.append({"step": name, "status": "FAILED", "seconds": round(time.time() - t0, 1),
                              "error": f"{type(exc).__name__}: {exc}"[:500]})
        run.step_name = name
        return run
    return deco


@step("1 environment check")
def env_check():
    import shutil
    import socket
    from common.llm_client import llm_status
    info = {"python": sys.version.split()[0], "disk_free_gb": round(shutil.disk_usage(ROOT).free / 1e9, 1),
            "llm": llm_status()}
    try:
        socket.create_connection(("data.cityofnewyork.us", 443), timeout=5).close()
        info["network"] = "ok"
    except OSError:
        info["network"] = "unavailable"
    for mod in ("pandas", "numpy", "pydantic", "sklearn", "pandera", "rapidfuzz", "phonenumbers", "yaml"):
        __import__(mod)
    print(json.dumps(info, indent=1))
    return info


@step("2 official tax source snapshots")
def sources():
    from project_a.tax_sources import snapshot_sources
    m = snapshot_sources()
    ok = sum(x["status"] == "ok" for x in m)
    print(f"{ok}/{len(m)} sources snapshotted (others: see manifest)")
    return {"ok": ok, "total": len(m)}


@step("3 generate Project A test suite")
def gen_tests():
    from project_a.test_case_generator import generate
    r = generate()
    print(json.dumps(r, indent=1))
    return r


def _pytest(path: str, label: str) -> dict:
    out = subprocess.run([sys.executable, "-m", "pytest", path, "-q", "-p", "no:cacheprovider", "-W",
                          "ignore::FutureWarning"], cwd=ROOT, capture_output=True, text=True)
    tail = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr[-300:]
    print(f"{label}: {tail}")
    (ROOT / "logs" / f"pytest_{label}.txt").write_text(out.stdout + out.stderr)
    if out.returncode != 0:
        raise RuntimeError(f"{label} tests failed: {tail}")
    return {"summary": tail}


@step("4 Project A tests")
def tests_a():
    return _pytest("project_a/tests", "project_a")


@step("5 Project A benchmark")
def bench_a():
    from project_a.benchmark import run
    r = run()
    for fy, engines in r["by_fy"].items():
        for name, e in engines.items():
            print(f"FY{fy} {name:<22} exact={e.get('rupee_exact_accuracy')} status={e['status']}")
    from project_a.llm_extractor_eval import run as eval_extractor
    ex = eval_extractor()  # reuses the stored real measurement unless LLM_REMEASURE=1
    print(f"LLM extractor: {ex.get('status')} {ex.get('counts', '')}")
    return {"generated_at": r["generated_at"]}


@step("6 Task A5 migration report")
def migration():
    from project_a.law_change_migration import build_report
    r = build_report()
    print(f"silently wrong if year-blind: {r['silent_failure_probe']['silently_wrong']}/{r['silent_failure_probe']['n_cases']}; "
          f"mutants caught {r['mutation_test']['caught']}/{r['mutation_test']['n_mutants']}, "
          f"compiled mutants caught {r['compiled_mutation_test']['caught']}/{r['compiled_mutation_test']['n_mutants']}")
    return {"ok": True}


@step("7 NYC 311 data (download if not cached)")
def data(skip_download: bool):
    from project_b import data_loader
    raw = list(data_loader.PB_RAW.glob("day_*.csv"))
    if not raw and data_loader.SAMPLE_PARQUET.exists():
        # fresh clone without raw CSVs: use the committed frozen snapshot (no network needed)
        print("no raw chunks: using the committed frozen sample snapshot", data_loader.SAMPLE_PARQUET.name)
        return {"processed": "frozen snapshot (raw chunks not present)"}
    if not skip_download:
        data_loader.download_sample()
    p = data_loader.build_processed(force=True)
    v = subprocess.run([sys.executable, "scripts/verify_data.py", "--no-split"], cwd=ROOT, capture_output=True, text=True)
    print(v.stdout[-1500:])
    if v.returncode:
        raise RuntimeError("raw chunk verification failed (see project_b/data/processed/raw_manifest.json)")
    return {"processed": str(p.relative_to(ROOT))}


@step("8 Project B profile")
def profile():
    from project_b.profiler import run_profile
    r = run_profile()
    print(f"{len(r['issues'])} checks over {r['sample_rows']:,} rows")
    return {"rows": r["sample_rows"]}


@step("9 Project B tests")
def tests_b():
    return _pytest("project_b/tests", "project_b")


@step("10 Project B detector benchmark")
def bench_b(quick: bool):
    from common.config import settings
    from project_b import benchmark, reference
    if quick:
        settings()["project_b"].update(n_eval_batches=2, n_clean_batches=2)
    reference.prepare(force=True)
    v = subprocess.run([sys.executable, "scripts/verify_data.py"], cwd=ROOT, capture_output=True, text=True)
    print(v.stdout[-700:])
    if v.returncode:
        raise RuntimeError("chunk/temporal-split verification failed after rebuilding pools")
    r = benchmark.run()
    return {"rows": len(r["results"]), "runtime_s": round(r["runtime_s"], 1)}


@step("11 Project B hybrid pipeline")
def hybrid():
    from project_b.cli import run_hybrid
    from project_b.scale_estimates import estimate
    r = run_hybrid()
    estimate()  # Task B6 projections from the measured results
    print(json.dumps(r["any_problem_row_level"], indent=1))
    return {"any_f1": r["any_problem_row_level"]["f1"]}


@step("12 regenerate reports")
def reports():
    out = subprocess.run([sys.executable, "scripts/generate_reports.py"], cwd=ROOT, capture_output=True, text=True)
    print(out.stdout[-1500:], out.stderr[-1500:])
    if out.returncode:
        raise RuntimeError("report generation failed")
    return {"ok": True}


@step("13 validate submission")
def validate():
    out = subprocess.run([sys.executable, "scripts/validate_submission.py"], cwd=ROOT, capture_output=True, text=True)
    print(out.stdout[-3000:], out.stderr[-1500:])
    if out.returncode:
        raise RuntimeError("validation found problems")
    return {"ok": True}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--from-step", type=int, default=1)
    a = ap.parse_args()
    ensure_dirs()
    if a.skip_download:
        os.environ["NYC_OFFLINE"] = "1"   # profiler then uses cached full-year counts only
    pipeline = [(1, env_check, ()), (2, sources, ()), (3, gen_tests, ()), (4, tests_a, ()), (5, bench_a, ()),
                (6, migration, ()), (7, data, (a.skip_download,)), (8, profile, ()), (9, tests_b, ()),
                (10, bench_b, (a.quick,)), (11, hybrid, ()), (12, reports, ()), (13, validate, ())]
    start = a.from_step
    if a.resume and CHECKPOINT.exists():
        prev = json.loads(CHECKPOINT.read_text()).get("steps", [])
        done = [int(s_["step"].split()[0]) for s_ in prev if s_["status"] in ("ok", "skipped")]
        n = 0
        while n + 1 in done:
            n += 1
        start = max(start, n + 1)
        print(f"resuming at step {start} (checkpoint: steps 1..{n} ok)")
    meta = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "argv": sys.argv[1:], "start_step": start}
    t0 = time.time()
    code = 1
    EXITCODE.unlink(missing_ok=True)   # a stale exit code must never describe this run
    try:
        for num, fn, args in pipeline:
            if num < start:
                STEPS.append({"step": fn.step_name, "status": "skipped", "seconds": 0.0,
                              "detail": "already completed in an earlier run (see checkpoint)"})
            else:
                fn(*args)
            _save_checkpoint(meta)
        code = 0 if all(s_["status"] in ("ok", "skipped") for s_ in STEPS) else 1
    finally:
        EXITCODE.write_text(f"{code}\n")
    summary = {"finished_at": time.strftime("%Y-%m-%d %H:%M:%S"), "total_seconds": round(time.time() - t0, 1),
               "quick_mode": a.quick, "skip_download": a.skip_download, "start_step": start, "steps": STEPS}
    (ROOT / "benchmarks" / "submission_summary.json").write_text(json.dumps(summary, indent=1, default=str))
    print("\n=== SUMMARY ===")
    for s in STEPS:
        print(f"{s['status']:<7} {s['seconds']:>7}s  {s['step']}" + (f"  -> {s.get('error')}" if s["status"] == "FAILED" else ""))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
