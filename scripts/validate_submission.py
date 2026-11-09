"""Automated submission validation. Exit code 0 only if every check passes.

Checks: required files, test-suite sizes, both financial years, 36 detector results, report presence and
two-page limits, LLM honesty labelling, no secrets, no TODO/FIXME, .env not tracked.
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, msg: str) -> None:
    RESULTS.append((bool(ok), msg))


REQUIRED = [
    "README.md", "requirements.txt", "pyproject.toml", ".env.example", ".gitignore", "Makefile", "run_all.py",
    "config/settings.yaml", "config/tax_rules_fy2024_25.yaml", "config/tax_rules_fy2025_26.yaml", "config/benchmark_config.yaml",
    "project_a/models.py", "project_a/rules_engine.py", "project_a/llm_query_engine.py", "project_a/llm_compiled_engine.py",
    "project_a/natural_language_parser.py", "project_a/tax_explainer.py", "project_a/rule_registry.py", "project_a/tax_sources.py",
    "project_a/test_case_generator.py", "project_a/benchmark.py", "project_a/law_change_migration.py", "project_a/cli.py",
    "project_a/tests/test_rules_engine.py", "project_a/tests/test_tax_boundaries.py", "project_a/tests/test_nlp_parser.py",
    "project_a/tests/test_llm_engines.py", "project_a/tests/test_regression.py",
    "project_a/data/tax_test_cases.json", "project_a/data/natural_language_cases.json",
    "project_b/data_loader.py", "project_b/profiler.py", "project_b/corruption_injector.py", "project_b/schema_detector.py",
    "project_b/statistical_detector.py", "project_b/ml_detector.py", "project_b/llm_detector.py", "project_b/hybrid_pipeline.py",
    "project_b/evaluation.py", "project_b/benchmark.py", "project_b/pii_detector.py", "project_b/cli.py",
    "project_b/tests/test_injector.py", "project_b/tests/test_schema_detector.py", "project_b/tests/test_statistical_detector.py",
    "project_b/tests/test_llm_detector.py", "project_b/tests/test_hybrid_pipeline.py",
    "reports/RESULTS_A.md", "reports/RESULTS_B.md", "reports/DECISION_MATRIX.md", "reports/AI_USAGE.md",
    "reports/FINAL_DESIGN_A.md", "reports/FINAL_DESIGN_B.md",
    "scripts/download_data.py", "scripts/generate_tax_tests.py", "scripts/run_benchmarks.py", "scripts/validate_submission.py",
    "project_a/compiled_rules/fy2024_25.compiled.json", "project_a/compiled_rules/fy2025_26.compiled.json",
]


def main() -> int:
    for f in REQUIRED:
        check((ROOT / f).exists() and (ROOT / f).stat().st_size > 0, f"file present: {f}")

    cases = json.loads((ROOT / "project_a/data/tax_test_cases.json").read_text())["cases"]
    check(len(cases) >= 200, f"tax test cases >= 200 (found {len(cases)})")
    fys = {c["input"]["financial_year"] for c in cases}
    check({"2024-25", "2025-26"} <= fys, "test cases cover FY 2024-25 and FY 2025-26")
    nl = json.loads((ROOT / "project_a/data/natural_language_cases.json").read_text())["cases"]
    check(len(nl) == 50, f"50 natural-language cases (found {len(nl)})")
    styles = " ".join(c["style"] for c in nl)
    check(all(k in styles for k in ("hinglish", "incomplete", "contradictory", "ambiguous")), "NL styles include Hinglish/incomplete/contradictory/ambiguous")
    with open(ROOT / "project_a/data/official_calculator_worksheet.csv", encoding="utf-8") as fh:
        ws = list(csv.DictReader(fh))
    verified = sum(r["status"] == "VERIFIED_MATCH" for r in ws)
    check(len(ws) == 30, f"official-calculator worksheet has 30 rows ({verified} verified, {len(ws) - verified} not verified)")
    checked = [r for r in ws if r["status"] in ("VERIFIED_MATCH", "MISMATCH")]
    missing_shots = [r["case_id"] for i, r in enumerate(ws, 1) if r in checked and not all(
        (ROOT / "evidence/official_calculator" / f"case_{i:02d}_{g}.png").exists() for g in ("old", "new"))]
    check(len(checked) >= 30 and not missing_shots,
          f"official calculator: {len(checked)}/30 cases checked on the portal ({verified} exact matches), "
          f"screenshots missing for {missing_shots}")

    for fy in ("2024_25", "2025_26"):
        art = json.loads((ROOT / f"project_a/compiled_rules/fy{fy}.compiled.json").read_text())
        check(art.get("review", {}).get("status") == "approved", f"compiled artifact fy{fy} approved")

    ba = ROOT / "benchmarks/project_a/benchmark_results.json"
    if ba.exists():
        b = json.loads(ba.read_text())
        for fy in ("2024-25", "2025-26"):
            e = b["by_fy"].get(fy, {})
            check(e.get("rules_engine", {}).get("rupee_exact_accuracy") == 1.0, f"rules engine 100% rupee-exact FY {fy}")
            llm = e.get("llm_query_engine", {})
            if not str(b["llm"]["mode"]).startswith("real"):
                check(str(llm.get("status", "")).startswith("NOT MEASURED") and llm.get("rupee_exact_accuracy") is None,
                      f"FY {fy}: query-time LLM reported as NOT MEASURED offline (no fabricated accuracy)")
            else:
                check(str(llm.get("status", "")).startswith("measured") and llm.get("n_cases", 0) > 0
                      and llm.get("rupee_exact_accuracy") is not None and llm.get("model") and llm.get("latency"),
                      f"FY {fy}: query-time LLM measured with real calls ({llm.get('model')}, n={llm.get('n_cases')}, "
                      f"exact={llm.get('rupee_exact_accuracy')})")
    else:
        check(False, "benchmarks/project_a/benchmark_results.json exists")
    check((ROOT / "benchmarks/project_a/migration_report.json").exists(), "Task A5 migration report exists")

    bb = ROOT / "benchmarks/project_b/detector_results.json"
    if bb.exists():
        r = json.loads(bb.read_text())
        pairs = {(x["problem"], x["family"] if x["family"] == "llm" else ("rule" if x["family"] == "rule" else "stat_ml"))
                 for x in r["results"]}
        check(len(pairs) == 36, f"36 detector/problem results (found {len(pairs)})")
        rc = r.get("config", {})
        check(rc.get("reference_months") == list(range(1, 10)) and rc.get("evaluation_months") == [10, 11, 12],
              f"detector results use the Jan-Sep reference / Oct-Dec evaluation split "
              f"(found {rc.get('reference_months')} / {rc.get('evaluation_months')})")
        llm_rows = [x for x in r["results"] if x["family"] == "llm"]
        if not str(r["llm"]["mode"]).startswith("real"):
            check(all(x["status"].startswith("NOT MEASURED") for x in llm_rows),
                  "LLM detector rows are labelled NOT MEASURED offline")
        else:
            ok = [x for x in llm_rows if x["status"].startswith("measured") and x.get("tp") is not None]
            check(len(ok) == 12, f"LLM detector rows measured with real calls: {len(ok)}/12 "
                                 f"(unmeasured: {[x['problem'] for x in llm_rows if x not in ok]})")
    else:
        check(False, "benchmarks/project_b/detector_results.json exists")
    check((ROOT / "benchmarks/project_b/hybrid_results.json").exists(), "hybrid results exist")
    check((ROOT / "project_b/data/reports/profile.json").exists(), "real-data profile exists")
    mf = ROOT / "project_b/data/processed/raw_manifest.json"
    if mf.exists():
        m = json.loads(mf.read_text())
        check(m["present"] == m["expected_chunks"] and not m["problems"],
              f"NYC chunks verified: {m['present']}/{m['expected_chunks']}, problems={m['problems'][:3]}")
        sp = m["split"]
        check(isinstance(sp.get("reference"), dict) and isinstance(sp.get("evaluation"), dict),
              f"temporal split verified: reference months {sp.get('reference', {}).get('months_present') if isinstance(sp.get('reference'), dict) else sp.get('reference')}, "
              f"evaluation months {sp.get('evaluation', {}).get('months_present') if isinstance(sp.get('evaluation'), dict) else sp.get('evaluation')}")
    else:
        check(False, "raw data manifest exists (run scripts/verify_data.py)")

    for doc in ("FINAL_DESIGN_A.md", "FINAL_DESIGN_B.md"):
        path = ROOT / "reports" / doc
        words = len(re.findall(r"\S+", path.read_text())) if path.exists() else 10**9
        check(words <= 1150, f"{doc} within two pages (~{words} words, limit 1,150)")

    secret = re.compile(r"AIza[0-9A-Za-z_\-]{30,}")
    todo = re.compile(r"\b(TODO|FIXME|XXX)\b")
    leaks, todos = [], []
    for p in ROOT.rglob("*"):
        if p.is_dir() or any(part in {".git", "__pycache__", "raw", "corrupted", "llm_cache"} for part in p.parts):
            continue
        if p.suffix not in {".py", ".md", ".yaml", ".yml", ".json", ".txt", ".toml", ".example", ""} or p.stat().st_size > 5e6:
            continue
        text = p.read_text(errors="ignore")
        if secret.search(text):
            leaks.append(str(p.relative_to(ROOT)))
        if p.suffix == ".py" and todo.search(text) and p.name != "validate_submission.py":
            todos.append(str(p.relative_to(ROOT)))
    check(not leaks, f"no API keys in files {leaks}")
    check(not todos, f"no TODO/FIXME in code {todos}")
    try:
        tracked = subprocess.run(["git", "ls-files", ".env"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        check(tracked == "", ".env is not tracked by git")
    except FileNotFoundError:
        pass
    check(".env" in (ROOT / ".gitignore").read_text().split(), ".env listed in .gitignore")

    for ok, msg in RESULTS:
        print(f"[{'PASS' if ok else 'FAIL'}] {msg}")
    failed = [m for ok, m in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    (ROOT / "benchmarks" / "validation.json").write_text(json.dumps(
        [{"ok": ok, "check": m} for ok, m in RESULTS], indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
